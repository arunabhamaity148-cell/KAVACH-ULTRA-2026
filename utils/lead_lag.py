"""
KAVACH-ULTRA 2026 — utils/lead_lag.py
Detects when Hyperliquid price leads Binance by >0.15% with volume confirmation.
Generates 'Front-Run' directional signals.
"""

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, Optional, List

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
    direction: str          # "LONG" | "SHORT"
    hl_price: float
    binance_price: float
    divergence_pct: float
    volume_confirmed: bool
    timestamp: float
    confidence: float       # 0–1


class LeadLagDetector:
    """
    Monitors rolling price history for both exchanges.
    Fires a Front-Run signal when:
      1. |HL_price - BN_price| / BN_price > LEAD_LAG_THRESHOLD_PCT
      2. HL volume in window >= LEAD_LAG_VOLUME_FACTOR × its own baseline
    """

    def __init__(self):
        # Rolling snapshots: symbol → source → deque of PriceSnapshot
        self._history: Dict[str, Dict[str, deque]] = {}
        self._active_signals: Dict[str, LeadLagSignal] = {}
        self._signal_history: List[LeadLagSignal] = []

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
        """
        Call this on every price tick.
        Returns a LeadLagSignal if front-run pattern detected, else None.
        """
        self._ensure_symbol(symbol)
        snap = PriceSnapshot(price=price, volume=volume, timestamp=time.time())
        self._history[symbol][source].append(snap)
        return self._evaluate(symbol)

    def _evaluate(self, symbol: str) -> Optional[LeadLagSignal]:
        bn_hist  = self._history[symbol]["binance"]
        hl_hist  = self._history[symbol]["hyperliquid"]

        if not bn_hist or not hl_hist:
            return None

        now = time.time()
        window = config.LEAD_LAG_WINDOW_SECONDS

        # Recent snapshots within the rolling window
        bn_recent  = [s for s in bn_hist  if now - s.timestamp <= window]
        hl_recent  = [s for s in hl_hist  if now - s.timestamp <= window]

        if not bn_recent or not hl_recent:
            return None

        bn_price_now = bn_recent[-1].price
        hl_price_now = hl_recent[-1].price

        if bn_price_now <= 0:
            return None

        divergence_pct = (hl_price_now - bn_price_now) / bn_price_now

        if abs(divergence_pct) < config.LEAD_LAG_THRESHOLD_PCT:
            # No significant divergence — clear any stale signal
            self._active_signals.pop(symbol, None)
            return None

        # ── Volume confirmation ──
        hl_recent_vol = sum(s.volume for s in hl_recent)
        hl_all_vol    = sum(s.volume for s in hl_hist)
        hl_avg_vol    = hl_all_vol / len(hl_hist) * len(hl_recent) if hl_hist else 0

        volume_confirmed = (
            hl_avg_vol > 0 and
            hl_recent_vol >= hl_avg_vol * config.LEAD_LAG_VOLUME_FACTOR
        )

        # ── Direction ──
        direction = "LONG" if divergence_pct > 0 else "SHORT"

        # ── Confidence score ──
        divergence_ratio = abs(divergence_pct) / config.LEAD_LAG_THRESHOLD_PCT
        confidence = min(divergence_ratio * 0.5 + (0.3 if volume_confirmed else 0), 1.0)

        signal = LeadLagSignal(
            symbol=symbol,
            direction=direction,
            hl_price=hl_price_now,
            binance_price=bn_price_now,
            divergence_pct=round(divergence_pct * 100, 4),
            volume_confirmed=volume_confirmed,
            timestamp=now,
            confidence=round(confidence, 3),
        )

        # Deduplicate: don't re-fire same direction within 60s
        prev = self._active_signals.get(symbol)
        if prev and prev.direction == direction and now - prev.timestamp < 60:
            return None

        self._active_signals[symbol] = signal
        self._signal_history.append(signal)

        logger.info(
            f"[LEAD-LAG] {symbol} FRONT-RUN {direction} | "
            f"HL={hl_price_now:.4f} BN={bn_price_now:.4f} "
            f"Div={signal.divergence_pct:+.3f}% "
            f"VolConfirm={volume_confirmed} Conf={confidence:.2f}"
        )

        return signal

    def get_current_status(self, symbol: str) -> Dict:
        """Returns current lead-lag status for dashboard display."""
        bn_hist  = self._history.get(symbol, {}).get("binance", deque())
        hl_hist  = self._history.get(symbol, {}).get("hyperliquid", deque())

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
        }

    def get_all_statuses(self) -> List[Dict]:
        symbols = list(self._history.keys())
        return [self.get_current_status(s) for s in symbols]
