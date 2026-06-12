"""
KAVACH-ULTRA 2026 — utils/lead_lag.py  [FIXED v2]
Fixes:
  - BUG #5: Divergence was returning 0.000% (attribute never set)
  - BUG #3: Confidence 97-100% (was returning raw value, no penalties)
  - BUG #7: Cooldown not working (timestamp comparison was wrong)
"""

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, Optional, List

from loguru import logger
import config


# ─── DATA STRUCTURES ─────────────────────────────────────────────────────────

@dataclass
class PriceSnapshot:
    price: float
    volume: float
    timestamp: float


@dataclass
class LeadLagSignal:
    symbol: str
    direction: str              # "LONG" | "SHORT"
    hl_price: float
    binance_price: float
    divergence_pct: float       # FIX #5: was always 0 — now correctly calculated
    volume_confirmed: bool
    timestamp: float
    confidence: float           # FIX #3: now 40–85% range, not 97–100%

    # FIX #4: extra fields for clear Telegram message
    is_arbitrage_signal: bool = True   # Always True for lead-lag
    sentiment_aligned: bool = True


# ─── LEAD-LAG DETECTOR ───────────────────────────────────────────────────────

class LeadLagDetector:
    """
    Monitors rolling price history for both exchanges.
    Fires a Front-Run signal when:
      1. |HL_price - BN_price| / BN_price > LEAD_LAG_THRESHOLD_PCT
      2. HL volume in window >= LEAD_LAG_VOLUME_FACTOR × its own baseline
    """

    def __init__(self):
        self._history: Dict[str, Dict[str, deque]] = {}
        self._active_signals: Dict[str, LeadLagSignal] = {}
        self._signal_history: List[LeadLagSignal] = []

        # FIX #7: proper cooldown tracking — symbol → last signal timestamp
        self._last_signal_time: Dict[str, float] = {}
        self.COOLDOWN_SECONDS = 300  # 5 minutes

    def _ensure_symbol(self, symbol: str):
        if symbol not in self._history:
            self._history[symbol] = {
                "binance": deque(maxlen=100),
                "hyperliquid": deque(maxlen=100),
            }

    def on_price_update(
        self,
        symbol: str,
        source: str,
        price: float,
        volume: float,
    ) -> Optional[LeadLagSignal]:
        self._ensure_symbol(symbol)
        snap = PriceSnapshot(price=price, volume=volume, timestamp=time.time())
        self._history[symbol][source].append(snap)
        return self._evaluate(symbol)

    def _evaluate(self, symbol: str) -> Optional[LeadLagSignal]:
        bn_hist = self._history[symbol]["binance"]
        hl_hist = self._history[symbol]["hyperliquid"]

        if not bn_hist or not hl_hist:
            return None

        now    = time.time()
        window = getattr(config, "LEAD_LAG_WINDOW_SECONDS", 30)

        # Get snapshots within rolling window
        bn_recent = [s for s in bn_hist if now - s.timestamp <= window]
        hl_recent = [s for s in hl_hist if now - s.timestamp <= window]

        if not bn_recent or not hl_recent:
            return None

        bn_price_now = bn_recent[-1].price
        hl_price_now = hl_recent[-1].price

        if bn_price_now <= 0:
            return None

        # ─────────────────────────────────────────────────────────────────────
        # FIX #5: DIVERGENCE CALCULATION
        # Old code: divergence_pct was set but then signal.divergence_pct
        #           was read from a different (unset) attribute in dashboard
        # Fix: calculate correctly here AND store in the dataclass
        # ─────────────────────────────────────────────────────────────────────
        raw_divergence = (hl_price_now - bn_price_now) / bn_price_now
        divergence_pct = round(raw_divergence * 100, 4)  # e.g. 0.1523 not 0.000

        threshold_pct = getattr(config, "LEAD_LAG_THRESHOLD_PCT", 0.0015)

        if abs(raw_divergence) < threshold_pct:
            self._active_signals.pop(symbol, None)
            return None

        # ── Volume confirmation ──────────────────────────────────────────────
        hl_recent_vol = sum(s.volume for s in hl_recent)
        hl_all_qty    = len(hl_hist)
        hl_avg_vol    = (sum(s.volume for s in hl_hist) / hl_all_qty) * len(hl_recent) if hl_all_qty > 0 else 0
        vol_factor    = getattr(config, "LEAD_LAG_VOLUME_FACTOR", 1.5)
        volume_confirmed = hl_avg_vol > 0 and hl_recent_vol >= hl_avg_vol * vol_factor

        # ── Direction ────────────────────────────────────────────────────────
        direction = "LONG" if raw_divergence > 0 else "SHORT"

        # ─────────────────────────────────────────────────────────────────────
        # FIX #3: REALISTIC CONFIDENCE ALGORITHM
        # Old: confidence = divergence_ratio * 0.5 + 0.3 → easily hits 0.95+
        # New: base 50%, with bounded bonuses/penalties → range 40–85%
        # ─────────────────────────────────────────────────────────────────────
        confidence = self._calculate_confidence(
            raw_divergence=raw_divergence,
            threshold=threshold_pct,
            volume_confirmed=volume_confirmed,
            bn_recent=bn_recent,
            hl_recent=hl_recent,
        )

        # ─────────────────────────────────────────────────────────────────────
        # FIX #7: COOLDOWN — proper timestamp check
        # Old bug: checked prev.direction == direction (direction could change)
        #          but used prev.timestamp from a stale signal object
        # Fix: track per-symbol last-fired time in a separate dict
        # ─────────────────────────────────────────────────────────────────────
        last_fired = self._last_signal_time.get(symbol, 0)
        elapsed    = now - last_fired
        if elapsed < self.COOLDOWN_SECONDS:
            remaining = int(self.COOLDOWN_SECONDS - elapsed)
            logger.debug(
                f"[LEAD-LAG] {symbol} cooldown active — {remaining}s remaining"
            )
            return None

        # ── Build signal ─────────────────────────────────────────────────────
        signal = LeadLagSignal(
            symbol=symbol,
            direction=direction,
            hl_price=hl_price_now,
            binance_price=bn_price_now,
            divergence_pct=divergence_pct,   # FIX #5: correct value now stored
            volume_confirmed=volume_confirmed,
            timestamp=now,
            confidence=confidence,           # FIX #3: realistic value
            is_arbitrage_signal=True,
        )

        # Update cooldown tracker and active signals
        self._last_signal_time[symbol] = now   # FIX #7
        self._active_signals[symbol]   = signal
        self._signal_history.append(signal)

        logger.info(
            f"[LEAD-LAG] {symbol} {direction} | "
            f"HL={hl_price_now:.4f} BN={bn_price_now:.4f} "
            f"Div={divergence_pct:+.4f}% "          # will now show real value
            f"VolConfirm={volume_confirmed} "
            f"Conf={confidence:.1f}%"
        )

        return signal

    # ─────────────────────────────────────────────────────────────────────────
    # FIX #3: MULTI-FACTOR CONFIDENCE — max realistic ~85%
    # ─────────────────────────────────────────────────────────────────────────
    def _calculate_confidence(
        self,
        raw_divergence: float,
        threshold: float,
        volume_confirmed: bool,
        bn_recent: list,
        hl_recent: list,
    ) -> float:
        """
        Multi-factor confidence calculation.
        Returns a percentage 0–100 (realistically 40–85).

        Factors:
          Base:               50%
          Divergence bonus:   up to +15%  (how much above threshold)
          Volume confirm:     +15% if yes, -5% if no
          Trend agreement:    +5%  if recent prices moving same direction
          Penalty — thin HL:  -10% if < 5 snapshots in window
        """
        score = 50.0

        # 1. Divergence bonus (capped at +15%)
        multiple = abs(raw_divergence) / threshold  # e.g. 1.5x = 0.15% vs 0.10%
        div_bonus = min((multiple - 1.0) * 10.0, 15.0)
        score += div_bonus

        # 2. Volume confirmation
        score += 15.0 if volume_confirmed else -5.0

        # 3. Trend agreement (both exchanges trending the same direction?)
        if len(bn_recent) >= 3 and len(hl_recent) >= 3:
            bn_trend = bn_recent[-1].price - bn_recent[0].price
            hl_trend = hl_recent[-1].price - hl_recent[0].price
            if (bn_trend > 0 and hl_trend > 0) or (bn_trend < 0 and hl_trend < 0):
                score += 5.0

        # 4. Thin order flow penalty
        if len(hl_recent) < 5:
            score -= 10.0

        # Hard cap: 40% minimum, 85% maximum
        return round(max(40.0, min(score, 85.0)), 1)

    # ─── STATUS / DASHBOARD HELPERS ───────────────────────────────────────────

    def get_current_status(self, symbol: str) -> Dict:
        bn_hist = self._history.get(symbol, {}).get("binance", deque())
        hl_hist = self._history.get(symbol, {}).get("hyperliquid", deque())

        bn_price = bn_hist[-1].price if bn_hist else 0.0
        hl_price = hl_hist[-1].price if hl_hist else 0.0

        # FIX #5: use same formula consistently
        divergence = ((hl_price - bn_price) / bn_price * 100) if bn_price > 0 else 0.0

        active = self._active_signals.get(symbol)

        # Cooldown remaining
        last_fired = self._last_signal_time.get(symbol, 0)
        cooldown_remaining = max(0, int(self.COOLDOWN_SECONDS - (time.time() - last_fired)))

        return {
            "symbol": symbol,
            "binance_price": bn_price,
            "hl_price": hl_price,
            "divergence_pct": round(divergence, 4),   # e.g. +0.1523
            "active_signal": active.direction if active else "NEUTRAL",
            "signal_confidence": active.confidence if active else 0.0,
            "cooldown_remaining": cooldown_remaining,
        }

    def get_all_statuses(self) -> List[Dict]:
        return [self.get_current_status(s) for s in self._history]

    def get_cooldown_status(self, symbol: str) -> int:
        """Returns seconds remaining in cooldown for a symbol (0 = ready)."""
        last_fired = self._last_signal_time.get(symbol, 0)
        return max(0, int(self.COOLDOWN_SECONDS - (time.time() - last_fired)))
