"""
KAVACH-ULTRA 2026 — core/risk_manager.py
Institutional-grade risk management:
  - Dynamic position sizing (1.5% / 3% risk per trade)
  - Daily loss limit enforcement
  - Time filter (09:00 – 00:00 IST)
  - SL/TP calculation with liquidity wall targeting
  - Regulatory pair filter
"""

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Dict, List

import aiohttp
from loguru import logger

import config
from utils.database import TradeDatabase


@dataclass
class TradeOrder:
    symbol: str
    direction: str           # "LONG" | "SHORT"
    entry_price: float
    stop_loss: float
    take_profit: float
    quantity: float          # In base asset
    usdt_risk: float         # Actual USDT at risk
    leverage: int
    risk_pct: float          # 0.015 or 0.030
    strategy: str            # "sweep" | "order_flow" | "funding" | "lead_lag"
    signal_confidence: float
    timestamp: float = field(default_factory=time.time)


@dataclass
class RiskState:
    daily_pnl: float = 0.0
    daily_loss: float = 0.0
    open_positions: int = 0
    total_exposure_usdt: float = 0.0
    trading_paused: bool = False
    pause_reason: str = ""
    session_start: float = field(default_factory=time.time)


class RiskManager:

    def __init__(self, db: TradeDatabase):
        self.db = db
        self.state = RiskState()
        self._balance: float = config.TOTAL_CAPITAL_USDT
        self._open_positions: Dict[str, TradeOrder] = {}
        self._lock = asyncio.Lock()
        self._session: Optional[aiohttp.ClientSession] = None

    async def start(self):
        self._session = aiohttp.ClientSession()
        await self._sync_balance()
        logger.info(f"[RISK] Manager started | Balance: ${self._balance:.2f} USDT")

    async def stop(self):
        if self._session:
            await self._session.close()

    # ─── PRE-TRADE CHECKS ────────────────────────────────────────────────────

    async def check_pre_trade(
        self,
        symbol: str,
        direction: str,
        confidence: float,
    ) -> tuple[bool, str]:
        """
        Full pre-trade validation.
        Returns (approved: bool, reason: str)
        """

        # 1. Regulatory filter
        if symbol in config.BANNED_PAIRS:
            return False, f"{symbol} is on the Indian regulatory exclusion list"

        # 2. Time filter (IST)
        if not self._is_trading_time():
            ist_now = datetime.now(config.IST)
            return False, f"Outside trading hours (09:00–00:00 IST). Current: {ist_now.strftime('%H:%M IST')}"

        # 3. Daily loss limit
        if await self._is_daily_loss_hit():
            return False, f"Daily loss limit ({config.DAILY_LOSS_LIMIT_PCT*100:.0f}%) reached. Trading paused for today."

        # 4. Max open positions
        if self.state.open_positions >= config.MAX_OPEN_POSITIONS:
            return False, f"Max open positions ({config.MAX_OPEN_POSITIONS}) reached"

        # 5. Already in this symbol
        if symbol in self._open_positions:
            return False, f"Already have an open position in {symbol}"

        # 6. Bot-level pause (black swan etc.)
        if self.state.trading_paused:
            return False, f"Trading paused: {self.state.pause_reason}"

        return True, "All pre-trade checks passed"

    def _is_trading_time(self) -> bool:
        """Check if current IST time is within 09:00 – 00:00."""
        now = datetime.now(config.IST)
        hour = now.hour
        # Allow 09:00 to 23:59 (inclusive)
        if config.TRADING_START_HOUR <= hour <= 23:
            return True
        # Midnight edge case: 00:00 is end, so hour == 0 is NOT allowed
        return False

    async def _is_daily_loss_hit(self) -> bool:
        limit = self._balance * config.DAILY_LOSS_LIMIT_PCT
        return self.state.daily_loss >= limit

    # ─── POSITION SIZING ─────────────────────────────────────────────────────

    def calculate_position(
        self,
        symbol: str,
        direction: str,
        entry_price: float,
        stop_loss: float,
        take_profit: float,
        signal_confidence: float,
        strategy: str,
        is_high_confidence: bool = False,
    ) -> Optional[TradeOrder]:
        """
        Kelly-influenced position sizing capped at risk limits.
        Returns None if RR is insufficient.
        """
        if entry_price <= 0 or stop_loss <= 0:
            return None

        # Risk percentage
        risk_pct = config.HIGH_CONF_RISK_PCT if is_high_confidence else config.DEFAULT_RISK_PCT

        # Validate SL direction
        if direction == "LONG" and stop_loss >= entry_price:
            logger.warning(f"[RISK] {symbol} LONG: SL ({stop_loss}) >= entry ({entry_price})")
            return None
        if direction == "SHORT" and stop_loss <= entry_price:
            logger.warning(f"[RISK] {symbol} SHORT: SL ({stop_loss}) <= entry ({entry_price})")
            return None

        # Risk/Reward check
        if take_profit:
            if direction == "LONG":
                rr = (take_profit - entry_price) / (entry_price - stop_loss)
            else:
                rr = (entry_price - take_profit) / (stop_loss - entry_price)
            if rr < 1.5:
                logger.debug(f"[RISK] {symbol} RR={rr:.2f} < 1.5, skipping")
                return None

        # USDT risk amount
        usdt_risk = self._balance * risk_pct

        # Distance to SL in price terms
        sl_distance = abs(entry_price - stop_loss)
        sl_distance_pct = sl_distance / entry_price

        if sl_distance_pct <= 0:
            return None

        # Position size in USDT (leveraged)
        position_usdt = usdt_risk / sl_distance_pct
        position_usdt = min(position_usdt, self._balance * config.LEVERAGE * 0.3)  # Max 30% of leveraged capital

        # Quantity in base asset
        quantity = position_usdt / entry_price

        return TradeOrder(
            symbol=symbol,
            direction=direction,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            quantity=round(quantity, 6),
            usdt_risk=round(usdt_risk, 2),
            leverage=config.LEVERAGE,
            risk_pct=risk_pct,
            strategy=strategy,
            signal_confidence=signal_confidence,
        )

    # ─── POSITION TRACKING ───────────────────────────────────────────────────

    async def register_open_position(self, order: TradeOrder):
        async with self._lock:
            self._open_positions[order.symbol] = order
            self.state.open_positions = len(self._open_positions)
            self.state.total_exposure_usdt += order.usdt_risk * order.leverage

        await self.db.log_trade_open(order)
        logger.info(
            f"[RISK] Position opened: {order.symbol} {order.direction} "
            f"Entry={order.entry_price} SL={order.stop_loss} TP={order.take_profit} "
            f"Qty={order.quantity} Risk=${order.usdt_risk}"
        )

    async def register_closed_position(
        self,
        symbol: str,
        exit_price: float,
        exit_reason: str,
    ):
        async with self._lock:
            order = self._open_positions.pop(symbol, None)
            if not order:
                return

            # PnL calculation
            if order.direction == "LONG":
                pnl = (exit_price - order.entry_price) / order.entry_price * order.usdt_risk * order.leverage
            else:
                pnl = (order.entry_price - exit_price) / order.entry_price * order.usdt_risk * order.leverage

            self.state.daily_pnl += pnl
            if pnl < 0:
                self.state.daily_loss += abs(pnl)

            self.state.open_positions = len(self._open_positions)
            self.state.total_exposure_usdt = max(
                0, self.state.total_exposure_usdt - order.usdt_risk * order.leverage
            )

        await self.db.log_trade_close(symbol, exit_price, pnl, exit_reason)
        logger.info(
            f"[RISK] Position closed: {symbol} | "
            f"Exit={exit_price} PnL=${pnl:+.2f} Reason={exit_reason}"
        )

    async def emergency_pause(self, reason: str):
        async with self._lock:
            self.state.trading_paused = True
            self.state.pause_reason = reason
        logger.critical(f"[RISK] ⚠️ EMERGENCY PAUSE: {reason}")

    async def resume_trading(self):
        async with self._lock:
            self.state.trading_paused = False
            self.state.pause_reason = ""
        logger.info("[RISK] Trading resumed")

    # ─── BALANCE SYNC ────────────────────────────────────────────────────────

    async def _sync_balance(self):
        """Sync actual balance from Binance REST API."""
        try:
            if not config.BINANCE_API_KEY:
                logger.warning("[RISK] No Binance API key. Using config capital.")
                return

            url = f"{config.BINANCE_REST}/fapi/v2/balance"
            import hmac, hashlib
            ts = int(time.time() * 1000)
            params = f"timestamp={ts}"
            sig = hmac.new(
                config.BINANCE_API_SECRET.encode(),
                params.encode(),
                hashlib.sha256,
            ).hexdigest()

            headers = {"X-MBX-APIKEY": config.BINANCE_API_KEY}
            async with self._session.get(
                f"{url}?{params}&signature={sig}",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as r:
                if r.status == 200:
                    data = await r.json()
                    for asset in data:
                        if asset.get("asset") == "USDT":
                            self._balance = float(asset.get("availableBalance", self._balance))
                            logger.info(f"[RISK] Synced balance: ${self._balance:.2f} USDT")
                            return
        except Exception as e:
            logger.warning(f"[RISK] Balance sync failed: {e}. Using ${self._balance:.2f}")

    def get_status(self) -> Dict:
        return {
            "balance_usdt": round(self._balance, 2),
            "daily_pnl": round(self.state.daily_pnl, 2),
            "daily_loss": round(self.state.daily_loss, 2),
            "daily_loss_limit": round(self._balance * config.DAILY_LOSS_LIMIT_PCT, 2),
            "loss_limit_pct_used": round(
                self.state.daily_loss / (self._balance * config.DAILY_LOSS_LIMIT_PCT) * 100, 1
            ) if self._balance > 0 else 0,
            "open_positions": self.state.open_positions,
            "exposure_usdt": round(self.state.total_exposure_usdt, 2),
            "trading_paused": self.state.trading_paused,
            "pause_reason": self.state.pause_reason,
            "is_trading_time": self._is_trading_time(),
        }

    def get_open_positions(self) -> List[Dict]:
        return [
            {
                "symbol": o.symbol,
                "direction": o.direction,
                "entry": o.entry_price,
                "sl": o.stop_loss,
                "tp": o.take_profit,
                "risk_usdt": o.usdt_risk,
                "strategy": o.strategy,
                "confidence": o.signal_confidence,
                "age_min": round((time.time() - o.timestamp) / 60, 1),
            }
            for o in self._open_positions.values()
        ]
