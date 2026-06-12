"""
KAVACH-ULTRA 2026 — main.py
Master async orchestrator. Dashboard moved to dashboard.py.
"""

import asyncio
import time
import sys
from typing import Optional

from loguru import logger
from rich.console import Console
from rich.live import Live

import config
from core.data_engine import DataEngine
from core.ai_brain import AIBrain
from core.risk_manager import RiskManager
from utils.database import TradeDatabase
from utils.telegram_bot import TelegramController
from utils.lead_lag import LeadLagDetector
from strategies.liquidity_sweep import LiquiditySweepStrategy
from strategies.order_flow import OrderFlowStrategy
from strategies.funding_squeeze import FundingSqueezeStrategy
from dashboard import build_dashboard


# ─── LOGGING SETUP ───────────────────────────────────────────────────────────

logger.remove()
logger.add(
    sys.stderr,
    level=config.LOG_LEVEL,
    format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | {message}",
    colorize=True,
)
logger.add(config.LOG_FILE, rotation="50 MB", retention="14 days", level="DEBUG")


# ─── EXECUTION ENGINE ────────────────────────────────────────────────────────

class ExecutionEngine:
    """
    Placeholder for live order execution.
    In production: connect to Binance Futures REST API.
    In paper/shadow mode: log only.
    """

    def __init__(self, mode: str = "PAPER"):
        self.mode = mode  # PAPER | LIVE

    async def place_order(self, order) -> bool:
        if self.mode == "PAPER":
            logger.info(
                f"[PAPER] Would execute: {order.symbol} {order.direction} "
                f"qty={order.quantity} entry~{order.entry_price:.4f} "
                f"SL={order.stop_loss:.4f} TP={order.take_profit:.4f}"
            )
            return True
        # LIVE mode: implement Binance Futures POST /fapi/v1/order here
        raise NotImplementedError("Live execution not yet enabled. Set mode=PAPER for safety.")

    async def close_position(self, symbol: str, direction: str, quantity: float) -> bool:
        close_dir = "SELL" if direction == "LONG" else "BUY"
        if self.mode == "PAPER":
            logger.info(f"[PAPER] Would close: {symbol} {close_dir} qty={quantity}")
            return True
        raise NotImplementedError("Live execution not yet enabled.")


# ─── KAVACH-ULTRA ORCHESTRATOR ───────────────────────────────────────────────

class KavachUltra:

    def __init__(self):
        # Core components
        self.db       = TradeDatabase()
        self.data_eng = DataEngine()
        self.ai_brain = AIBrain(self.db)
        self.risk_mgr = RiskManager(self.db)
        self.telegram = TelegramController()
        self.executor = ExecutionEngine(mode="PAPER")  # Change to LIVE when ready

        # Strategy engines
        state = self.data_eng.state
        self.lead_lag = LeadLagDetector()
        self.sweep    = LiquiditySweepStrategy(state)
        self.ob_flow  = OrderFlowStrategy(state)
        self.funding  = FundingSqueezeStrategy(state)

        # Signal processing lock
        self._signal_lock = asyncio.Lock()
        self._running = False

        # Dashboard stats
        self._signals_today = 0
        self._signals_executed = 0

    async def start(self):
        self._running = True
        logger.info(f"🛡 {config.BOT_NAME} v{config.VERSION} starting...")

        # Initialise all components
        await self.db.start()
        await self.risk_mgr.start()
        await self.ai_brain.start()

        # Register Telegram
        self.telegram.register_components(
            risk_manager=self.risk_mgr,
            ai_brain=self.ai_brain,
            funding_strategy=self.funding,
            close_all_callback=self._close_all_positions,
        )
        await self.telegram.start()

        # Register price update callback for lead-lag + strategy evaluation
        self.data_eng.register_callback(self._on_price_update)

        # Launch data engine (non-blocking tasks)
        await self.data_eng.start()

        # Start funding rate monitor
        asyncio.create_task(self._funding_monitor_loop(), name="funding_monitor")

        # Start candle strategy evaluation loop
        asyncio.create_task(self._strategy_eval_loop(), name="strategy_eval")

        await self.telegram.send_message(
            f"🛡 <b>KAVACH-ULTRA 2026 Started</b>\n"
            f"Mode: {self.executor.mode} | Capital: ${config.TOTAL_CAPITAL_USDT:,.0f}"
        )

        logger.success(f"✅ {config.BOT_NAME} fully operational")

    async def stop(self):
        self._running = False
        await self.telegram.send_message("⚠️ KAVACH-ULTRA shutting down...")
        await self.data_eng.stop()
        await self.ai_brain.stop()
        await self.risk_mgr.stop()
        await self.telegram.stop()
        await self.db.stop()
        logger.info("KAVACH-ULTRA stopped.")

    # ─── CALLBACKS ───────────────────────────────────────────────────────────

    async def _on_price_update(
        self,
        symbol: str,
        source: str,
        price: float,
        volume: float,
    ):
        """Called on every price tick from any exchange."""
        # Lead-Lag evaluation
        signal = self.lead_lag.on_price_update(symbol, source, price, volume)
        if signal and signal.volume_confirmed:
            entry_price = self.data_eng.state.get_price(symbol, "binance") or price

            # Fallback SL/TP for Lead-Lag signals
            sl, tp = self._compute_sl_tp(symbol, signal.direction, entry_price)

            await self._process_signal(
                symbol=signal.symbol,
                direction=signal.direction,
                entry_price=entry_price,
                confidence=signal.confidence,
                strategy="lead_lag",
                sl=sl,
                tp=tp,
            )

    async def _funding_monitor_loop(self):
        """Check funding rates for squeeze opportunities."""
        while self._running:
            try:
                for symbol in config.ACTIVE_PAIRS:
                    fd = self.data_eng.state.funding.get(symbol)
                    if fd:
                        signal = self.funding.on_funding_update(fd)
                        if signal:
                            await self._process_signal(
                                symbol=signal.symbol,
                                direction=signal.direction,
                                entry_price=signal.entry_price,
                                confidence=signal.confidence,
                                strategy="funding",
                                sl=None,
                                tp=None,
                            )
            except Exception as e:
                logger.warning(f"[FUNDING LOOP] Error: {e}")
            await asyncio.sleep(60)

    async def _strategy_eval_loop(self):
        """Evaluate candle-based strategies every 30 seconds."""
        while self._running:
            try:
                for symbol in config.ACTIVE_PAIRS:
                    # Liquidity sweep
                    sweep_sig = self.sweep.evaluate(symbol)
                    if sweep_sig:
                        await self._process_signal(
                            symbol=sweep_sig.symbol,
                            direction=sweep_sig.direction,
                            entry_price=sweep_sig.entry_price,
                            confidence=sweep_sig.confidence,
                            strategy="sweep",
                            sl=sweep_sig.stop_loss,
                            tp=sweep_sig.take_profit,
                        )

                    # Order flow imbalance
                    ob_sig = self.ob_flow.evaluate(symbol)
                    if ob_sig and ob_sig.confluence:
                        price = self.data_eng.state.get_price(symbol, "binance") or 0
                        await self._process_signal(
                            symbol=ob_sig.symbol,
                            direction=ob_sig.direction,
                            entry_price=price,
                            confidence=ob_sig.confidence,
                            strategy="order_flow",
                            sl=None,
                            tp=None,
                        )

            except Exception as e:
                logger.warning(f"[STRATEGY LOOP] Error: {e}")
            await asyncio.sleep(30)

    # ─── SIGNAL PIPELINE ─────────────────────────────────────────────────────

    async def _process_signal(
        self,
        symbol: str,
        direction: str,
        entry_price: float,
        confidence: float,
        strategy: str,
        sl: Optional[float],
        tp: Optional[float],
    ):
        async with self._signal_lock:
            self._signals_today += 1

            # ── Pre-trade risk check ──
            trade_ok, trade_reason = await self.risk_mgr.check_pre_trade(
                symbol, direction, confidence
            )
            if not trade_ok:
                logger.debug(f"[SIGNAL] {symbol} {direction} REJECTED by risk: {trade_reason}")
                await self.db.log_signal(
                    symbol, strategy, direction, confidence,
                    False, trade_reason, 0.0, entry_price, False, trade_reason
                )
                return

            # ── AI Sentiment Approval ──
            approval = await self.ai_brain.approve_signal(symbol, direction, confidence)
            if not approval.approved:
                logger.info(f"[SIGNAL] {symbol} {direction} REJECTED by AI: {approval.reason}")
                await self.db.log_signal(
                    symbol, strategy, direction, confidence,
                    False, approval.reason, approval.sentiment_score, entry_price,
                    False, approval.reason,
                )
                return

            # AI confidence boost
            final_confidence = min(confidence + approval.confidence_boost, 1.0)
            is_high_conf = final_confidence >= 0.75

            # ── Compute SL/TP if not provided ──
            if sl is None or tp is None:
                sl, tp = self._compute_sl_tp(symbol, direction, entry_price)
                if not sl or not tp:
                    logger.debug(f"[SIGNAL] {symbol} — Could not compute SL/TP, skipping")
                    return

            # ── Position sizing ──
            order = self.risk_mgr.calculate_position(
                symbol=symbol,
                direction=direction,
                entry_price=entry_price,
                stop_loss=sl,
                take_profit=tp,
                signal_confidence=final_confidence,
                strategy=strategy,
                is_high_confidence=is_high_conf,
            )
            if not order:
                logger.debug(f"[SIGNAL] {symbol} — Position sizing failed (poor RR?)")
                return

            # ── Execute ──
            executed = await self.executor.place_order(order)
            if executed:
                await self.risk_mgr.register_open_position(order)
                await self.telegram.notify_trade_opened(order)
                self._signals_executed += 1

                await self.db.log_signal(
                    symbol, strategy, direction, final_confidence,
                    True, approval.reason, approval.sentiment_score,
                    entry_price, True, ""
                )
                logger.success(
                    f"✅ TRADE EXECUTED: {symbol} {direction} | "
                    f"Entry={entry_price:.4f} SL={sl:.4f} TP={tp:.4f} "
                    f"Risk=${order.usdt_risk:.2f} Strategy={strategy}"
                )

    def _compute_sl_tp(
        self,
        symbol: str,
        direction: str,
        entry_price: float,
    ) -> tuple[Optional[float], Optional[float]]:
        """
        Fallback SL/TP from order book liquidity walls and ATR-style estimate.
        Used when strategies don't provide explicit SL/TP.
        """
        ob = self.data_eng.state.get_order_book(symbol, "binance")
        walls = ob.get_liquidity_walls() if ob else []

        if direction == "LONG":
            # SL: 0.5% below entry (fallback)
            sl = entry_price * 0.995
            # TP: nearest ask-side wall above entry
            ask_walls = [w for w in walls if w["side"] == "ask" and w["price"] > entry_price]
            tp = min(w["price"] for w in ask_walls) if ask_walls else entry_price * 1.015
        else:
            sl = entry_price * 1.005
            bid_walls = [w for w in walls if w["side"] == "bid" and w["price"] < entry_price]
            tp = max(w["price"] for w in bid_walls) if bid_walls else entry_price * 0.985

        # Validate RR
        if direction == "LONG":
            rr = (tp - entry_price) / (entry_price - sl) if (entry_price - sl) > 0 else 0
        else:
            rr = (entry_price - tp) / (sl - entry_price) if (sl - entry_price) > 0 else 0

        if rr < 1.5:
            return None, None

        return sl, tp

    async def _close_all_positions(self, reason: str = "MANUAL"):
        """Emergency close all open positions."""
        positions = self.risk_mgr.get_open_positions()
        for pos in positions:
            price = self.data_eng.state.get_price(pos["symbol"], "binance") or pos["entry"]
            await self.executor.close_position(pos["symbol"], pos["direction"], 0)
            await self.risk_mgr.register_closed_position(pos["symbol"], price, reason)
            await self.telegram.notify_trade_closed(pos["symbol"], price, 0, reason)
        logger.warning(f"[EXEC] Closed {len(positions)} positions. Reason: {reason}")


# ─── MAIN ENTRY POINT ────────────────────────────────────────────────────────

async def main():
    bot = KavachUltra()
    console = Console()

    try:
        await bot.start()

        # Live dashboard refresh loop
        with Live(
            build_dashboard(bot, console),
            console=console,
            refresh_per_second=1,
            screen=True,
        ) as live:
            while True:
                await asyncio.sleep(1)
                live.update(build_dashboard(bot, console))

    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received")
    except Exception as e:
        logger.critical(f"Fatal error: {e}", exc_info=True)
    finally:
        await bot.stop()


if __name__ == "__main__":
    asyncio.run(main())
