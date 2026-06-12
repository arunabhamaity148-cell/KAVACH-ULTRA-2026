"""
KAVACH-ULTRA 2026 — strategies/funding_squeeze.py
Detects when retail is over-leveraged via extreme funding rates.
Trades the inevitable liquidation cascade / funding squeeze.
"""

import time
from collections import deque
from dataclasses import dataclass
from typing import Optional, Dict, List

from loguru import logger
import config
from core.data_engine import MarketState, FundingData


@dataclass
class FundingSqueezeSignal:
    symbol: str
    direction: str          # "LONG" (shorts get squeezed) | "SHORT" (longs get squeezed)
    funding_rate: float     # Current rate
    consecutive_count: int  # Periods of extreme funding in same direction
    avg_funding: float      # Average over consecutive window
    squeeze_magnitude: str  # "MILD" | "EXTREME"
    entry_price: float
    confidence: float
    timestamp: float


class FundingSqueezeStrategy:
    """
    Logic:
    - Extreme POSITIVE funding (longs pay shorts) = over-leveraged longs
      → Eventual LONG squeeze → trade SHORT
    - Extreme NEGATIVE funding (shorts pay longs) = over-leveraged shorts
      → Eventual SHORT squeeze → trade LONG
    """

    def __init__(self, state: MarketState):
        self.state = state
        # Rolling funding rate history per symbol: deque of (rate, timestamp)
        self._funding_history: Dict[str, deque] = {}
        self._recent_signals: Dict[str, FundingSqueezeSignal] = {}

    def _get_history(self, symbol: str) -> deque:
        if symbol not in self._funding_history:
            self._funding_history[symbol] = deque(maxlen=50)
        return self._funding_history[symbol]

    def on_funding_update(self, fd: FundingData) -> Optional[FundingSqueezeSignal]:
        """Called every time a new funding rate is received."""
        hist = self._get_history(fd.symbol)
        hist.append((fd.rate, fd.timestamp))
        return self.evaluate(fd.symbol)

    def evaluate(self, symbol: str) -> Optional[FundingSqueezeSignal]:
        """Evaluate current funding state for squeeze setup."""
        hist = self._get_history(symbol)
        if len(hist) < config.FUNDING_SQUEEZE_WINDOW:
            return None

        fd = self.state.funding.get(symbol)
        if not fd:
            return None

        threshold = config.FUNDING_EXTREME_THRESHOLD
        window = config.FUNDING_SQUEEZE_WINDOW

        recent = list(hist)[-window:]
        rates = [r for r, t in recent]

        # ── Over-leveraged LONGS (positive extreme) → trade SHORT ──
        if all(r > threshold for r in rates):
            avg_rate = sum(rates) / len(rates)
            magnitude = "EXTREME" if avg_rate > threshold * 2 else "MILD"
            entry = self.state.get_price(symbol, "binance") or 0.0
            confidence = self._calc_confidence(rates, threshold, "SHORT")

            signal = self._make_signal(
                symbol=symbol,
                direction="SHORT",
                rates=rates,
                avg_rate=avg_rate,
                magnitude=magnitude,
                entry=entry,
                confidence=confidence,
            )
            return self._deduplicate(symbol, signal)

        # ── Over-leveraged SHORTS (negative extreme) → trade LONG ──
        if all(r < -threshold for r in rates):
            avg_rate = sum(rates) / len(rates)
            magnitude = "EXTREME" if avg_rate < -threshold * 2 else "MILD"
            entry = self.state.get_price(symbol, "binance") or 0.0
            confidence = self._calc_confidence(rates, threshold, "LONG")

            signal = self._make_signal(
                symbol=symbol,
                direction="LONG",
                rates=rates,
                avg_rate=avg_rate,
                magnitude=magnitude,
                entry=entry,
                confidence=confidence,
            )
            return self._deduplicate(symbol, signal)

        return None

    def _make_signal(
        self,
        symbol: str,
        direction: str,
        rates: List[float],
        avg_rate: float,
        magnitude: str,
        entry: float,
        confidence: float,
    ) -> FundingSqueezeSignal:
        signal = FundingSqueezeSignal(
            symbol=symbol,
            direction=direction,
            funding_rate=rates[-1],
            consecutive_count=len(rates),
            avg_funding=round(avg_rate, 6),
            squeeze_magnitude=magnitude,
            entry_price=entry,
            confidence=confidence,
            timestamp=time.time(),
        )
        logger.info(
            f"[FUNDING] {symbol} {direction} SQUEEZE | "
            f"Rate={rates[-1]:.4%} avg={avg_rate:.4%} "
            f"Consecutive={len(rates)} Magnitude={magnitude} Conf={confidence:.2f}"
        )
        return signal

    def _calc_confidence(
        self,
        rates: List[float],
        threshold: float,
        direction: str,
    ) -> float:
        score = 0.5

        # Consecutive periods bonus
        score += min(len(rates) * 0.05, 0.2)

        # Rate magnitude bonus
        avg_abs = sum(abs(r) for r in rates) / len(rates)
        multiple = avg_abs / threshold
        score += min((multiple - 1) * 0.1, 0.2)

        # Escalating rates = stronger signal
        if direction == "SHORT":
            if rates[-1] > rates[0]:
                score += 0.1
        else:
            if rates[-1] < rates[0]:
                score += 0.1

        return round(min(score, 1.0), 3)

    def _deduplicate(
        self,
        symbol: str,
        signal: FundingSqueezeSignal,
    ) -> Optional[FundingSqueezeSignal]:
        prev = self._recent_signals.get(symbol)
        if prev and prev.direction == signal.direction:
            # Re-fire only if magnitude increased or confidence improved significantly
            if signal.confidence <= prev.confidence + 0.05:
                return None
        self._recent_signals[symbol] = signal
        return signal

    def get_funding_summary(self) -> List[Dict]:
        """Returns funding rate overview for all tracked symbols."""
        summary = []
        for symbol, hist in self._funding_history.items():
            if not hist:
                continue
            rates = [r for r, t in list(hist)[-8:]]  # Last 8 periods
            avg = sum(rates) / len(rates) if rates else 0
            current = rates[-1] if rates else 0
            threshold = config.FUNDING_EXTREME_THRESHOLD

            status = "NORMAL"
            if current > threshold:
                status = "EXTREME_LONG"
            elif current < -threshold:
                status = "EXTREME_SHORT"

            summary.append({
                "symbol": symbol,
                "current_rate": round(current, 6),
                "avg_rate_8h": round(avg, 6),
                "annualized_pct": round(current * 3 * 365 * 100, 2),
                "status": status,
            })

        return sorted(summary, key=lambda x: abs(x["current_rate"]), reverse=True)
