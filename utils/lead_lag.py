#!/usr/bin/env python3
"""
KAVACH-ULTRA 2026 — utils/lead_lag.py  [v3 FIXED]
Fixes:
  - BUG #4: Signal spam — proper 5-min cooldown per (pair, direction)
  - BUG #4: VolConfirm always False — fixed volume baseline calculation
  - BUG #4: Min divergence raised to 0.25%
  - BUG #4: Min confidence threshold 65%
  - Added: quality_score (0-100) combining all factors
"""

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, Optional, List, Tuple

from loguru import logger
import config


@dataclass
class PriceSnapshot:
    price: float
    volume: float
    timestamp: float


@dataclass
class LeadLagSignal:
    symbol: str
    direction: str
    hl_price: float
    binance_price: float
    divergence_pct: float
    volume_confirmed: bool
    timestamp: float
    confidence: float        # 40–85%
    quality_score: float     # 0–100 combined score
    is_arbitrage_signal: bool = True


class LeadLagDetector:

    # FIX #4: raise minimum thresholds
    MIN_DIVERGENCE_PCT   = 0.0025   # 0.25% minimum (was 0.15%)
    MIN_CONFIDENCE       = 65.0     # 65% minimum (was 50%)
    COOLDOWN_SECONDS     = 300      # 5 minutes per (pair, direction)

    def __init__(self):
        self._history: Dict[str, Dict[str, deque]] = {}

        # FIX #4: cooldown per (symbol, direction) — not just symbol
        # key: "BTCUSDT_LONG" or "BTCUSDT_SHORT"
        self._last_signal_time: Dict[str, float] = {}

        self._active_signals: Dict[str, LeadLagSignal] = {}
        self._signal_history: List[LeadLagSignal] = []

    def _ensure(self, symbol: str):
        if symbol not in self._history:
            self._history[symbol] = {
                "binance":      deque(maxlen=120),
                "hyperliquid":  deque(maxlen=120),
            }

    def on_price_update(
        self,
        symbol: str,
        source: str,
        price: float,
        volume: float,
    ) -> Optional[LeadLagSignal]:
        self._ensure(symbol)
        self._history[symbol][source].append(
            PriceSnapshot(price=price, volume=volume, timestamp=time.time())
        )
        return self._evaluate(symbol)

    def _evaluate(self, symbol: str) -> Optional[LeadLagSignal]:
        bn_hist = self._history[symbol]["binance"]
        hl_hist = self._history[symbol]["hyperliquid"]

        if not bn_hist or not hl_hist:
            return None

        now    = time.time()
        window = getattr(config, "LEAD_LAG_WINDOW_SECONDS", 30)

        bn_recent = [s for s in bn_hist if now - s.timestamp <= window]
        hl_recent = [s for s in hl_hist if now - s.timestamp <= window]

        if not bn_recent or not hl_recent:
            return None

        bn_price = bn_recent[-1].price
        hl_price = hl_recent[-1].price

        if bn_price <= 0:
            return None

        # ── Divergence ────────────────────────────────────────────────────
        raw_div = (hl_price - bn_price) / bn_price
        div_pct = round(raw_div * 100, 4)

        # FIX #4: minimum divergence 0.25%
        if abs(raw_div) < self.MIN_DIVERGENCE_PCT:
            return None

        direction = "LONG" if raw_div > 0 else "SHORT"

        # ── FIX #4: Cooldown per (symbol, direction) ──────────────────────
        cooldown_key = f"{symbol}_{direction}"
        elapsed = now - self._last_signal_time.get(cooldown_key, 0)
        if elapsed < self.COOLDOWN_SECONDS:
            logger.debug(
                f"[LEAD-LAG] {symbol} {direction} cooldown: "
                f"{int(self.COOLDOWN_SECONDS - elapsed)}s remaining"
            )
            return None

        # ── FIX #4: Volume confirmation — fixed calculation ────────────────
        # Old bug: compared recent_vol to baseline * len(recent) incorrectly
        # Fix: compare average volume per snapshot
        volume_confirmed = self._check_volume(symbol, hl_recent, bn_recent)

        # ── Confidence (40–85%) ───────────────────────────────────────────
        confidence = self._calc_confidence(
            raw_div, volume_confirmed, bn_recent, hl_recent
        )

        # FIX #4: minimum confidence 65%
        if confidence < self.MIN_CONFIDENCE:
            logger.debug(
                f"[LEAD-LAG] {symbol} {direction} confidence {confidence:.1f}% "
                f"< minimum {self.MIN_CONFIDENCE}% — skipping"
            )
            return None

        # ── Quality score (0–100) ─────────────────────────────────────────
        quality = self._calc_quality(raw_div, volume_confirmed, confidence)

        signal = LeadLagSignal(
            symbol=symbol,
            direction=direction,
            hl_price=hl_price,
            binance_price=bn_price,
            divergence_pct=div_pct,
            volume_confirmed=volume_confirmed,
            timestamp=now,
            confidence=confidence,
            quality_score=quality,
            is_arbitrage_signal=True,
        )

        # Update cooldown
        self._last_signal_time[cooldown_key] = now
        self._active_signals[symbol] = signal
        self._signal_history.append(signal)

        logger.info(
            f"[LEAD-LAG] {symbol} {direction} | "
            f"Div={div_pct:+.4f}% "
            f"Vol={'✓' if volume_confirmed else '✗'} "
            f"Conf={confidence:.1f}% "
            f"Quality={quality:.0f}/100"
        )

        return signal

    def _check_volume(
        self,
        symbol: str,
        hl_recent: List[PriceSnapshot],
        bn_recent: List[PriceSnapshot],
    ) -> bool:
        """
        FIX #4: Volume confirmation.
        Check if BOTH exchanges show volume above their rolling average.
        Old bug: multiplied avg_per_snap by len(recent) → always False
        Fix: compare avg recent volume per snapshot to overall avg per snapshot
        """
        vol_factor = getattr(config, "LEAD_LAG_VOLUME_FACTOR", 1.2)

        def _check_one(hist_deque, recent_snaps) -> bool:
            all_snaps = list(hist_deque)
            if len(all_snaps) < 5:
                return False  # Not enough history

            # Average volume per snapshot in full history
            full_avg = sum(s.volume for s in all_snaps) / len(all_snaps)
            if full_avg <= 0:
                return False

            # Average volume per snapshot in recent window
            recent_avg = sum(s.volume for s in recent_snaps) / len(recent_snaps)

            return recent_avg >= full_avg * vol_factor

        hl_hist = self._history[symbol]["hyperliquid"]
        bn_hist = self._history[symbol]["binance"]

        hl_ok = _check_one(hl_hist, hl_recent)
        bn_ok = _check_one(bn_hist, bn_recent)

        # Both must confirm
        return hl_ok and bn_ok

    def _calc_confidence(
        self,
        raw_div: float,
        vol_confirmed: bool,
        bn_recent: List[PriceSnapshot],
        hl_recent: List[PriceSnapshot],
    ) -> float:
        score = 50.0

        # Divergence bonus (capped +15%)
        multiple = abs(raw_div) / self.MIN_DIVERGENCE_PCT
        score += min((multiple - 1.0) * 8.0, 15.0)

        # Volume bonus/penalty
        score += 15.0 if vol_confirmed else -5.0

        # Trend agreement bonus
        if len(bn_recent) >= 3 and len(hl_recent) >= 3:
            bn_trend = bn_recent[-1].price - bn_recent[0].price
            hl_trend = hl_recent[-1].price - hl_recent[0].price
            if (bn_trend > 0 and hl_trend > 0) or (bn_trend < 0 and hl_trend < 0):
                score += 5.0

        # Thin data penalty
        if len(hl_recent) < 3:
            score -= 10.0

        return round(max(40.0, min(score, 85.0)), 1)

    def _calc_quality(
        self,
        raw_div: float,
        vol_confirmed: bool,
        confidence: float,
    ) -> float:
        """
        Quality score 0–100 combining all factors.
        Used in Telegram message for quick visual assessment.
        """
        # Divergence component (0–40 points)
        div_score = min(abs(raw_div) / 0.005 * 40, 40.0)  # Max at 0.5% div

        # Volume component (0–30 points)
        vol_score = 30.0 if vol_confirmed else 10.0

        # Confidence component (0–30 points)
        conf_score = (confidence - 40) / 45 * 30  # Map 40–85% to 0–30

        return round(min(div_score + vol_score + conf_score, 100.0), 1)

    def get_current_status(self, symbol: str) -> Dict:
        bn_hist = self._history.get(symbol, {}).get("binance", deque())
        hl_hist = self._history.get(symbol, {}).get("hyperliquid", deque())

        bn_price = bn_hist[-1].price if bn_hist else 0.0
        hl_price = hl_hist[-1].price if hl_hist else 0.0
        divergence = ((hl_price - bn_price) / bn_price * 100) if bn_price > 0 else 0.0

        active = self._active_signals.get(symbol)

        return {
            "symbol": symbol,
            "binance_price": bn_price,
            "hl_price": hl_price,
            "divergence_pct": round(divergence, 4),
            "active_signal": active.direction if active else "NEUTRAL",
            "signal_confidence": active.confidence if active else 0.0,
            "quality_score": active.quality_score if active else 0.0,
        }

    def get_all_statuses(self) -> List[Dict]:
        return [self.get_current_status(s) for s in self._history]

    def get_cooldown_status(self, symbol: str, direction: str) -> int:
        key = f"{symbol}_{direction}"
        return max(0, int(self.COOLDOWN_SECONDS - (time.time() - self._last_signal_time.get(key, 0))))
