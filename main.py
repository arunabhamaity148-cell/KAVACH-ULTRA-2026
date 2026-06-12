"""
KAVACH-ULTRA 2026 — main.py
Master async orchestrator + Rich terminal dashboard.
Coordinates: Data Engine → Lead-Lag → Strategies → AI Brain → Risk Manager → Execution
"""

import asyncio
import time
import sys
from datetime import datetime
from typing import Optional

from loguru import logger
from rich.console import Console
from rich.live import Live
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.columns import Columns
from rich import box

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
        self.ai_brain = AIBrain()
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
            await self._process_signal(
                symbol=signal.symbol,
                direction=signal.direction,
                entry_price=self.data_eng.state.get_price(symbol, "binance") or price,
                confidence=signal.confidence,
                strategy="lead_lag",
                sl=None,  # Risk manager will compute from OB
                tp=None,
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


# ─── RICH TERMINAL DASHBOARD ─────────────────────────────────────────────────

def build_dashboard(bot: KavachUltra, console: Console) -> Layout:
    """Construct the terminal dashboard layout using Rich."""

    now_ist = datetime.now(config.IST).strftime("%Y-%m-%d %H:%M:%S IST")
    risk_status  = bot.risk_mgr.get_status()
    positions    = bot.risk_mgr.get_open_positions()
    sentiment    = bot.ai_brain.get_sentiment()
    ll_statuses  = bot.lead_lag.get_all_statuses()
    funding_sum  = bot.funding.get_funding_summary()

    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="middle"),
        Layout(name="bottom", size=8),
    )
    layout["middle"].split_row(
        Layout(name="left"),
        Layout(name="center"),
        Layout(name="right"),
    )

    # ── Header ──
    header_text = Text(justify="center")
    header_text.append("🛡 KAVACH-ULTRA 2026 ", style="bold cyan")
    header_text.append(f"| {now_ist} ", style="dim")
    paused = "⛔ PAUSED" if risk_status["trading_paused"] else "✅ LIVE"
    header_text.append(paused, style="bold red" if risk_status["trading_paused"] else "bold green")
    layout["header"].update(Panel(header_text, box=box.HORIZONTALS))

    # ── Risk & PnL panel ──
    risk_table = Table(box=box.MINIMAL, show_header=False, padding=(0, 1))
    risk_table.add_column("Key", style="dim")
    risk_table.add_column("Value", style="bold")
    pnl_style = "green" if risk_status["daily_pnl"] >= 0 else "red"
    risk_table.add_row("Balance",   f"${risk_status['balance_usdt']:,.2f}")
    risk_table.add_row("Daily PnL", Text(f"${risk_status['daily_pnl']:+.2f}", style=pnl_style))
    risk_table.add_row("Daily Loss", f"${risk_status['daily_loss']:.2f} / ${risk_status['daily_loss_limit']:.2f} ({risk_status['loss_limit_pct_used']:.0f}%)")
    risk_table.add_row("Positions", f"{risk_status['open_positions']}/{config.MAX_OPEN_POSITIONS}")
    risk_table.add_row("Exposure",  f"${risk_status['exposure_usdt']:,.2f}")
    risk_table.add_row("Signals ↑", f"{bot._signals_today} generated | {bot._signals_executed} executed")
    layout["left"].update(Panel(risk_table, title="[bold cyan]RISK & PnL", border_style="cyan"))

    # ── AI Sentiment panel ──
    ai_panel_content = Text()
    if sentiment:
        score = sentiment.score
        label_colors = {
            "VERY_BEARISH": "red", "BEARISH": "orange3",
            "NEUTRAL": "yellow", "BULLISH": "green", "VERY_BULLISH": "bright_green",
        }
        color = label_colors.get(sentiment.label, "white")
        ai_panel_content.append(f"  Score: ", style="dim")
        ai_panel_content.append(f"{score:+.1f}", style=f"bold {color}")
        ai_panel_content.append(f" / 10\n")
        ai_panel_content.append(f"  Label: ", style="dim")
        ai_panel_content.append(f"{sentiment.label}\n", style=f"bold {color}")
        ai_panel_content.append(f"  Conf:  {sentiment.confidence:.0%}\n", style="dim")
        if sentiment.black_swan:
            ai_panel_content.append(f"\n  ⚠️ BLACK SWAN\n  {sentiment.black_swan_reason}", style="bold red blink")
        elif sentiment.key_headlines:
            ai_panel_content.append("\n  Key News:\n", style="dim")
            for h in sentiment.key_headlines[:2]:
                short_h = h[:55] + "…" if len(h) > 55 else h
                ai_panel_content.append(f"  • {short_h}\n", style="dim")
    else:
        ai_panel_content.append("  Initializing...", style="dim italic")

    layout["center"].update(Panel(ai_panel_content, title="[bold yellow]AI SENTIMENT", border_style="yellow"))

    # ── Lead-Lag panel ──
    ll_table = Table(box=box.MINIMAL, show_header=True, padding=(0, 1))
    ll_table.add_column("Pair", style="cyan", width=12)
    ll_table.add_column("Div%", justify="right", width=8)
    ll_table.add_column("Signal", justify="center", width=9)
    ll_table.add_column("Conf", justify="right", width=6)

    for stat in ll_statuses[:6]:
        div = stat["divergence_pct"]
        sig = stat["active_signal"]
        sig_style = "green" if sig == "LONG" else ("red" if sig == "SHORT" else "dim")
        div_style = "green" if div > 0 else ("red" if div < 0 else "dim")
        ll_table.add_row(
            stat["symbol"].replace("USDT", ""),
            Text(f"{div:+.3f}", style=div_style),
            Text(sig, style=sig_style),
            f"{stat['signal_confidence']:.2f}",
        )

    layout["right"].update(Panel(ll_table, title="[bold magenta]LEAD-LAG (HL vs BN)", border_style="magenta"))

    # ── Bottom: Open positions + Funding ──
    bottom_cols = []

    # Open positions table
    pos_table = Table(box=box.MINIMAL, show_header=True, padding=(0, 1), title="Open Positions")
    pos_table.add_column("Pair", style="cyan")
    pos_table.add_column("Dir", justify="center")
    pos_table.add_column("Entry", justify="right")
    pos_table.add_column("SL", justify="right")
    pos_table.add_column("TP", justify="right")
    pos_table.add_column("Risk$", justify="right")
    pos_table.add_column("Strat", style="dim")

    if positions:
        for p in positions:
            dir_style = "green" if p["direction"] == "LONG" else "red"
            pos_table.add_row(
                p["symbol"].replace("USDT", ""),
                Text(p["direction"], style=dir_style),
                f"{p['entry']:.3f}",
                f"{p['sl']:.3f}",
                f"{p['tp']:.3f}",
                f"${p['risk_usdt']:.0f}",
                p["strategy"],
            )
    else:
        pos_table.add_row("[dim]No open positions[/dim]", "", "", "", "", "", "")

    # Funding heatmap table
    fund_table = Table(box=box.MINIMAL, show_header=True, padding=(0, 1), title="Funding Rates")
    fund_table.add_column("Pair", style="cyan")
    fund_table.add_column("Rate", justify="right")
    fund_table.add_column("Ann%", justify="right")
    fund_table.add_column("Status", justify="center")

    for item in funding_sum[:5]:
        status_style = {
            "EXTREME_LONG": "red", "EXTREME_SHORT": "green", "NORMAL": "dim"
        }.get(item["status"], "dim")
        fund_table.add_row(
            item["symbol"].replace("USDT", ""),
            f"{item['current_rate']:.4%}",
            f"{item['annualized_pct']:+.1f}%",
            Text(item["status"].replace("_", " "), style=status_style),
        )

    layout["bottom"].update(Panel(
        Columns([pos_table, fund_table], equal=True, expand=True),
        border_style="dim",
    ))

    return layout


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
