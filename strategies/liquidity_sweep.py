"""
KAVACH-ULTRA 2026 — strategies/liquidity_sweep.py
Detects institutional stop-loss hunting:
  1. Identifies previous day high/low as liquidity zones.
  2. Waits for a 'sweep' candle that violates the zone.
  3. Confirms a reversal before entering in the opposite direction.
"""

import time
from dataclasses import dataclass
from typing import Optional, List, Dict

import numpy as np
from loguru import logger

import config
from core.data_engine import MarketState, PriceBar


@dataclass
class LiquidityZone:
    symbol: str
    level: float
    zone_type: str       # "PDH" | "PDL" | "RANGE_HIGH" | "RANGE_LOW"
    strength: int        # Times this level was tested
    created_at: float


@dataclass
class SweepSignal:
    symbol: str
    direction: str       # "LONG" (swept lows → buy) | "SHORT" (swept highs → sell)
    entry_price: float
    stop_loss: float     # Behind sweep wick
    take_profit: float   # Next liquidity wall
    zone_swept: LiquidityZone
    sweep_wick_low: float
    sweep_wick_high: float
    confidence: float
    timestamp: float


class LiquiditySweepStrategy:
    """
    ICT-style liquidity sweep entry strategy.
    Monitors price action for stop-hunt patterns and enters on reversal.
    """

    def __init__(self, state: MarketState):
        self.state = state
        self._zones: Dict[str, List[LiquidityZone]] = {}
        self._last_zone_update: Dict[str, float] = {}
        self._recent_signals: Dict[str, SweepSignal] = {}

    # ─── ZONE IDENTIFICATION ─────────────────────────────────────────────────

    def _update_liquidity_zones(self, symbol: str) -> List[LiquidityZone]:
        """Identify current liquidity zones from candle history."""
        candles = list(self.state.candles.get(symbol, []))
        if len(candles) < config.SWEEP_LOOKBACK_CANDLES:
            return []

        recent = candles[-config.SWEEP_LOOKBACK_CANDLES:]
        zones: List[LiquidityZone] = []

        # Previous session high/low (use 96 candles = ~24h on 15m TF)
        prev_candles = recent[:-4]  # Exclude last 4 candles (current session)
        if not prev_candles:
            return []

        pdh = max(c.high for c in prev_candles)
        pdl = min(c.low  for c in prev_candles)

        zones.append(LiquidityZone(
            symbol=symbol, level=pdh, zone_type="PDH",
            strength=self._count_tests(recent, pdh, tolerance_pct=0.001),
            created_at=time.time(),
        ))
        zones.append(LiquidityZone(
            symbol=symbol, level=pdl, zone_type="PDL",
            strength=self._count_tests(recent, pdl, tolerance_pct=0.001),
            created_at=time.time(),
        ))

        # Range extremes from last 24 candles
        micro_candles = candles[-24:]
        range_high = max(c.high for c in micro_candles)
        range_low  = min(c.low  for c in micro_candles)

        if range_high != pdh:
            zones.append(LiquidityZone(
                symbol=symbol, level=range_high, zone_type="RANGE_HIGH",
                strength=self._count_tests(recent, range_high, tolerance_pct=0.0015),
                created_at=time.time(),
            ))
        if range_low != pdl:
            zones.append(LiquidityZone(
                symbol=symbol, level=range_low, zone_type="RANGE_LOW",
                strength=self._count_tests(recent, range_low, tolerance_pct=0.0015),
                created_at=time.time(),
            ))

        self._zones[symbol] = zones
        self._last_zone_update[symbol] = time.time()
        return zones

    def _count_tests(
        self,
        candles: List[PriceBar],
        level: float,
        tolerance_pct: float = 0.001,
    ) -> int:
        """Count how many candles touched/approached a level."""
        tolerance = level * tolerance_pct
        count = 0
        for c in candles:
            if abs(c.high - level) <= tolerance or abs(c.low - level) <= tolerance:
                count += 1
        return count

    # ─── SWEEP DETECTION ─────────────────────────────────────────────────────

    def evaluate(self, symbol: str) -> Optional[SweepSignal]:
        """
        Check if the latest candle is a sweep candle.
        Returns a SweepSignal if pattern confirmed, else None.
        """
        candles = list(self.state.candles.get(symbol, []))
        if len(candles) < 5:
            return None

        # Update zones every 4 candles (~1 hour on 15m TF) or if missing
        if (
            symbol not in self._zones or
            time.time() - self._last_zone_update.get(symbol, 0) > 3600
        ):
            self._update_liquidity_zones(symbol)

        zones = self._zones.get(symbol, [])
        if not zones:
            return None

        last   = candles[-1]
        prev   = candles[-2]

        # Calculate average body size for wick multiplier check
        bodies = [abs(c.close - c.open) for c in candles[-20:]]
        avg_body = np.mean(bodies) if bodies else 0
        if avg_body <= 0:
            return None

        for zone in zones:
            signal = self._check_sweep(symbol, last, prev, zone, avg_body, candles)
            if signal:
                # Deduplicate: don't fire same direction within 4 candles
                prev_sig = self._recent_signals.get(symbol)
                if prev_sig and prev_sig.direction == signal.direction:
                    if time.time() - prev_sig.timestamp < 3600:
                        continue
                self._recent_signals[symbol] = signal
                return signal

        return None

    def _check_sweep(
        self,
        symbol: str,
        candle: PriceBar,
        prev_candle: PriceBar,
        zone: LiquidityZone,
        avg_body: float,
        candles: List[PriceBar],
    ) -> Optional[SweepSignal]:
        """
        Check if `candle` swept `zone` and then reversed.

        Bullish sweep: wick pierces below zone (low < zone.level),
                       but candle closes ABOVE zone.level.
        Bearish sweep: wick pierces above zone (high > zone.level),
                       but candle closes BELOW zone.level.
        """
        level = zone.level
        body  = abs(candle.close - candle.open)

        # Wick significance check
        lower_wick = candle.open - candle.low if candle.close >= candle.open else candle.close - candle.low
        upper_wick = candle.high - candle.open if candle.close <= candle.open else candle.high - candle.close
        lower_wick = max(lower_wick, 0)
        upper_wick = max(upper_wick, 0)

        # ── Bullish sweep (stop-hunt lows → buy) ──
        if (
            candle.low < level and
            candle.close > level and
            lower_wick >= avg_body * config.SWEEP_WICK_MULTIPLIER
        ):
            reversal_pct = (candle.close - candle.low) / candle.low
            if reversal_pct < config.SWEEP_REVERSAL_PCT:
                return None

            # SL = just below the sweep wick
            sl = candle.low * (1 - 0.002)

            # TP = next liquidity wall above entry
            tp = self._find_next_tp(symbol, candle.close, "LONG", candles)

            if not tp or tp <= candle.close:
                return None

            rr = (tp - candle.close) / (candle.close - sl)
            if rr < 1.5:  # Require at least 1.5:1 RR
                return None

            confidence = self._calc_confidence(zone, reversal_pct, body, avg_body)

            logger.info(
                f"[SWEEP] {symbol} BULLISH SWEEP at zone {zone.zone_type}={level:.2f} | "
                f"Low={candle.low:.2f} Close={candle.close:.2f} "
                f"SL={sl:.2f} TP={tp:.2f} RR={rr:.1f}x Conf={confidence:.2f}"
            )

            return SweepSignal(
                symbol=symbol, direction="LONG",
                entry_price=candle.close, stop_loss=sl, take_profit=tp,
                zone_swept=zone,
                sweep_wick_low=candle.low, sweep_wick_high=candle.high,
                confidence=confidence, timestamp=time.time(),
            )

        # ── Bearish sweep (stop-hunt highs → sell) ──
        if (
            candle.high > level and
            candle.close < level and
            upper_wick >= avg_body * config.SWEEP_WICK_MULTIPLIER
        ):
            reversal_pct = (candle.high - candle.close) / candle.high
            if reversal_pct < config.SWEEP_REVERSAL_PCT:
                return None

            sl = candle.high * (1 + 0.002)
            tp = self._find_next_tp(symbol, candle.close, "SHORT", candles)

            if not tp or tp >= candle.close:
                return None

            rr = (candle.close - tp) / (sl - candle.close)
            if rr < 1.5:
                return None

            confidence = self._calc_confidence(zone, reversal_pct, body, avg_body)

            logger.info(
                f"[SWEEP] {symbol} BEARISH SWEEP at zone {zone.zone_type}={level:.2f} | "
                f"High={candle.high:.2f} Close={candle.close:.2f} "
                f"SL={sl:.2f} TP={tp:.2f} RR={rr:.1f}x Conf={confidence:.2f}"
            )

            return SweepSignal(
                symbol=symbol, direction="SHORT",
                entry_price=candle.close, stop_loss=sl, take_profit=tp,
                zone_swept=zone,
                sweep_wick_low=candle.low, sweep_wick_high=candle.high,
                confidence=confidence, timestamp=time.time(),
            )

        return None

    def _find_next_tp(
        self,
        symbol: str,
        entry: float,
        direction: str,
        candles: List[PriceBar],
    ) -> Optional[float]:
        """
        Find the next liquidity wall (cluster of highs/lows) beyond entry.
        Uses swing point analysis on recent candles.
        """
        # Build swing highs and lows from recent 48 candles
        swing_highs = []
        swing_lows  = []
        window = candles[-48:] if len(candles) >= 48 else candles

        for i in range(2, len(window) - 2):
            c = window[i]
            if (c.high > window[i-1].high and c.high > window[i-2].high and
                    c.high > window[i+1].high and c.high > window[i+2].high):
                swing_highs.append(c.high)
            if (c.low < window[i-1].low and c.low < window[i-2].low and
                    c.low < window[i+1].low and c.low < window[i+2].low):
                swing_lows.append(c.low)

        # Also check order book liquidity walls
        ob = self.state.get_order_book(symbol, "binance")
        if ob:
            walls = ob.get_liquidity_walls()
            for wall in walls:
                if direction == "LONG" and wall["side"] == "ask" and wall["price"] > entry:
                    swing_highs.append(wall["price"])
                elif direction == "SHORT" and wall["side"] == "bid" and wall["price"] < entry:
                    swing_lows.append(wall["price"])

        if direction == "LONG":
            candidates = [h for h in swing_highs if h > entry * 1.003]
            return min(candidates) if candidates else None
        else:
            candidates = [l for l in swing_lows if l < entry * 0.997]
            return max(candidates) if candidates else None

    def _calc_confidence(
        self,
        zone: LiquidityZone,
        reversal_pct: float,
        body: float,
        avg_body: float,
    ) -> float:
        score = 0.5
        # Zone strength bonus
        score += min(zone.strength * 0.05, 0.2)
        # Reversal strength bonus
        score += min(reversal_pct / 0.01 * 0.1, 0.15)
        # Candle body significance
        if body >= avg_body:
            score += 0.1
        return round(min(score, 1.0), 3)

    def get_active_zones(self, symbol: str) -> List[LiquidityZone]:
        """Return current liquidity zones for dashboard display."""
        return self._zones.get(symbol, [])
