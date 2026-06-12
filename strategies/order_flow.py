"""
KAVACH-ULTRA 2026 — strategies/order_flow.py
Institutional order book analysis:
  1. Bid/Ask imbalance → directional bias.
  2. Spoofing detection → warns of fake liquidity.
  3. Absorbs both Binance and Hyperliquid order books for confluence.
"""

import time
from dataclasses import dataclass
from typing import Optional, List, Dict

from loguru import logger
import config
from core.data_engine import MarketState, OrderBook


@dataclass
class OrderFlowSignal:
    symbol: str
    direction: str          # "LONG" | "SHORT"
    imbalance_ratio: float  # 0.0–1.0 (bid fraction)
    binance_imbalance: float
    hl_imbalance: float
    confluence: bool        # Both exchanges agree
    spoofing_detected: bool
    spoofing_details: List[Dict]
    liquidity_walls: List[Dict]  # Top walls in trade direction
    confidence: float
    timestamp: float


class OrderFlowStrategy:
    """
    Analyzes real-time L2 order book from both exchanges.
    Generates a directional signal when imbalance > OB_IMBALANCE_THRESHOLD.
    """

    def __init__(self, state: MarketState):
        self.state = state
        self._recent_signals: Dict[str, OrderFlowSignal] = {}
        self._imbalance_history: Dict[str, List[float]] = {}

    def evaluate(self, symbol: str) -> Optional[OrderFlowSignal]:
        """
        Evaluate current order book state for a signal.
        Returns OrderFlowSignal if conditions met.
        """
        bn_ob  = self.state.get_order_book(symbol, "binance")
        hl_ob  = self.state.get_order_book(symbol, "hyperliquid")

        if not bn_ob:
            return None

        bn_imbalance = bn_ob.imbalance
        hl_imbalance = hl_ob.imbalance if hl_ob else bn_imbalance  # Fallback

        # Weighted composite (Binance heavier as it's deeper)
        composite = bn_imbalance * 0.6 + hl_imbalance * 0.4

        # Track history for trend
        hist = self._imbalance_history.setdefault(symbol, [])
        hist.append(composite)
        if len(hist) > 20:
            hist.pop(0)

        # Check threshold
        if composite < config.OB_IMBALANCE_THRESHOLD and composite > (1 - config.OB_IMBALANCE_THRESHOLD):
            return None

        direction = "LONG" if composite > config.OB_IMBALANCE_THRESHOLD else "SHORT"
        confluence = self._check_confluence(bn_imbalance, hl_imbalance, direction)

        # Spoofing check
        spoof_bn = bn_ob.detect_spoofing()
        spoof_hl = hl_ob.detect_spoofing() if hl_ob else []
        spoof_all = spoof_bn + spoof_hl
        spoofing_detected = len(spoof_all) > 0

        # Warn on spoofing: if the 'large' orders are on the direction we want to trade,
        # they might be fake — reduce confidence
        if spoofing_detected:
            # Check if spoof orders are supporting our direction (suspicious)
            supporting_spoofs = [
                s for s in spoof_all
                if (direction == "LONG" and s["side"] == "bid") or
                   (direction == "SHORT" and s["side"] == "ask")
            ]
            if supporting_spoofs:
                logger.warning(
                    f"[ORDER FLOW] {symbol} Spoofing detected supporting {direction} "
                    f"— signal confidence reduced. Orders: {supporting_spoofs[:2]}"
                )

        # Liquidity walls in trade direction
        walls = bn_ob.get_liquidity_walls()
        relevant_walls = [
            w for w in walls
            if (direction == "LONG" and w["side"] == "ask") or
               (direction == "SHORT" and w["side"] == "bid")
        ]

        # Confidence calculation
        confidence = self._calc_confidence(
            composite, confluence, spoofing_detected, hist, direction
        )

        # Deduplicate: same direction within 5 minutes
        prev = self._recent_signals.get(symbol)
        if prev and prev.direction == direction and time.time() - prev.timestamp < 300:
            return None

        signal = OrderFlowSignal(
            symbol=symbol,
            direction=direction,
            imbalance_ratio=round(composite, 4),
            binance_imbalance=round(bn_imbalance, 4),
            hl_imbalance=round(hl_imbalance, 4),
            confluence=confluence,
            spoofing_detected=spoofing_detected,
            spoofing_details=spoof_all[:5],
            liquidity_walls=relevant_walls[:3],
            confidence=confidence,
            timestamp=time.time(),
        )

        self._recent_signals[symbol] = signal

        logger.info(
            f"[ORDER FLOW] {symbol} {direction} | "
            f"Imbalance={composite:.3f} (BN={bn_imbalance:.3f} HL={hl_imbalance:.3f}) "
            f"Confluence={confluence} Spoof={spoofing_detected} Conf={confidence:.2f}"
        )

        return signal

    def _check_confluence(
        self,
        bn_imb: float,
        hl_imb: float,
        direction: str,
    ) -> bool:
        """Both exchanges must agree on direction."""
        threshold = config.OB_IMBALANCE_THRESHOLD - 0.05  # Slightly looser for HL
        if direction == "LONG":
            return bn_imb > config.OB_IMBALANCE_THRESHOLD and hl_imb > threshold
        else:
            return bn_imb < (1 - config.OB_IMBALANCE_THRESHOLD) and hl_imb < (1 - threshold)

    def _calc_confidence(
        self,
        composite: float,
        confluence: bool,
        spoofing: bool,
        history: List[float],
        direction: str,
    ) -> float:
        score = 0.4

        # Imbalance strength
        if direction == "LONG":
            strength = (composite - config.OB_IMBALANCE_THRESHOLD) / (1 - config.OB_IMBALANCE_THRESHOLD)
        else:
            strength = (config.OB_IMBALANCE_THRESHOLD - composite) / config.OB_IMBALANCE_THRESHOLD
        score += min(strength * 0.3, 0.25)

        # Confluence bonus
        if confluence:
            score += 0.15

        # Trend (imbalance worsening = stronger signal)
        if len(history) >= 5:
            recent_avg = sum(history[-5:]) / 5
            older_avg  = sum(history[:-5]) / max(len(history) - 5, 1)
            if direction == "LONG" and recent_avg > older_avg:
                score += 0.1
            elif direction == "SHORT" and recent_avg < older_avg:
                score += 0.1

        # Spoofing penalty
        if spoofing:
            score -= 0.15

        return round(max(0.0, min(score, 1.0)), 3)

    def get_imbalance_snapshot(self, symbol: str) -> Dict:
        """Returns current imbalance data for dashboard."""
        bn_ob  = self.state.get_order_book(symbol, "binance")
        hl_ob  = self.state.get_order_book(symbol, "hyperliquid")

        bn_imb = bn_ob.imbalance if bn_ob else 0.5
        hl_imb = hl_ob.imbalance if hl_ob else 0.5

        walls_bn = bn_ob.get_liquidity_walls() if bn_ob else []
        spoof_bn = bn_ob.detect_spoofing() if bn_ob else []

        return {
            "symbol": symbol,
            "binance_imbalance": round(bn_imb, 4),
            "hl_imbalance": round(hl_imb, 4),
            "composite": round(bn_imb * 0.6 + hl_imb * 0.4, 4),
            "top_walls": walls_bn[:4],
            "spoof_orders": spoof_bn[:3],
        }
