"""
KAVACH-ULTRA 2026 — utils/telegram_bot.py
Full Telegram control interface.
Commands: /status /positions /panic_close /resume /funding /sentiment /help
"""

import asyncio
from typing import Optional, TYPE_CHECKING

from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes
from loguru import logger

import config

if TYPE_CHECKING:
    from core.risk_manager import RiskManager
    from core.ai_brain import AIBrain
    from strategies.funding_squeeze import FundingSqueezeStrategy


class TelegramController:

    def __init__(self):
        self._app: Optional[Application] = None
        self._risk: Optional["RiskManager"] = None
        self._ai_brain: Optional["AIBrain"] = None
        self._funding: Optional["FundingSqueezeStrategy"] = None
        self._close_all_callback = None

    def register_components(
        self,
        risk_manager: "RiskManager",
        ai_brain: "AIBrain",
        funding_strategy: "FundingSqueezeStrategy",
        close_all_callback,
    ):
        self._risk    = risk_manager
        self._ai_brain = ai_brain
        self._funding = funding_strategy
        self._close_all_callback = close_all_callback

    async def start(self):
        if not config.TELEGRAM_BOT_TOKEN:
            logger.warning("[TELEGRAM] No bot token. Telegram control disabled.")
            return

        self._app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()

        # Register commands
        self._app.add_handler(CommandHandler("start",       self._cmd_start))
        self._app.add_handler(CommandHandler("help",        self._cmd_help))
        self._app.add_handler(CommandHandler("status",      self._cmd_status))
        self._app.add_handler(CommandHandler("positions",   self._cmd_positions))
        self._app.add_handler(CommandHandler("panic_close", self._cmd_panic_close))
        self._app.add_handler(CommandHandler("resume",      self._cmd_resume))
        self._app.add_handler(CommandHandler("funding",     self._cmd_funding))
        self._app.add_handler(CommandHandler("sentiment",   self._cmd_sentiment))
        self._app.add_handler(CommandHandler("pause",       self._cmd_pause))

        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling(drop_pending_updates=True)
        logger.success("[TELEGRAM] Bot started ✓")

    async def stop(self):
        if self._app:
            await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()

    async def send_message(self, text: str, parse_mode: str = "HTML"):
        if not self._app or not config.TELEGRAM_CHAT_ID:
            return
        try:
            await self._app.bot.send_message(
                chat_id=config.TELEGRAM_CHAT_ID,
                text=text,
                parse_mode=parse_mode,
            )
        except Exception as e:
            logger.warning(f"[TELEGRAM] Send error: {e}")

    def _is_authorized(self, update: Update) -> bool:
        return str(update.effective_chat.id) == str(config.TELEGRAM_CHAT_ID)

    # ─── COMMAND HANDLERS ────────────────────────────────────────────────────

    async def _cmd_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            return
        await update.message.reply_text(
            f"🛡 <b>KAVACH-ULTRA 2026</b>\n"
            f"Bot is running. Use /help for commands.",
            parse_mode="HTML",
        )

    async def _cmd_help(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            return
        msg = (
            "🛡 <b>KAVACH-ULTRA Commands</b>\n\n"
            "/status — Bot health & daily PnL\n"
            "/positions — Open positions\n"
            "/panic_close — Close ALL positions immediately\n"
            "/pause — Pause new trades\n"
            "/resume — Resume trading\n"
            "/funding — Extreme funding rate pairs\n"
            "/sentiment — Current AI sentiment\n"
        )
        await update.message.reply_text(msg, parse_mode="HTML")

    async def _cmd_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            return
        if not self._risk:
            await update.message.reply_text("Risk manager not connected.")
            return

        s = self._risk.get_status()
        pnl_icon = "🟢" if s["daily_pnl"] >= 0 else "🔴"

        msg = (
            f"🛡 <b>KAVACH-ULTRA Status</b>\n\n"
            f"💰 Balance: <code>${s['balance_usdt']:,.2f}</code>\n"
            f"{pnl_icon} Daily PnL: <code>${s['daily_pnl']:+.2f}</code>\n"
            f"⚠️ Daily Loss: <code>${s['daily_loss']:.2f}</code> / "
            f"<code>${s['daily_loss_limit']:.2f}</code> "
            f"({s['loss_limit_pct_used']:.0f}%)\n"
            f"📊 Open Positions: <code>{s['open_positions']}/{config.MAX_OPEN_POSITIONS}</code>\n"
            f"💼 Exposure: <code>${s['exposure_usdt']:,.2f}</code>\n"
            f"⏰ Trading: {'✅ Active' if s['is_trading_time'] else '⛔ Off Hours'}\n"
            f"🔒 Paused: {'YES — ' + s['pause_reason'] if s['trading_paused'] else 'No'}"
        )
        await update.message.reply_text(msg, parse_mode="HTML")

    async def _cmd_positions(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            return
        if not self._risk:
            return

        positions = self._risk.get_open_positions()
        if not positions:
            await update.message.reply_text("📭 No open positions.")
            return

        lines = ["📊 <b>Open Positions</b>\n"]
        for p in positions:
            icon = "🟢" if p["direction"] == "LONG" else "🔴"
            lines.append(
                f"{icon} <b>{p['symbol']}</b> {p['direction']}\n"
                f"  Entry: <code>{p['entry']:.4f}</code>\n"
                f"  SL: <code>{p['sl']:.4f}</code> | TP: <code>{p['tp']:.4f}</code>\n"
                f"  Risk: <code>${p['risk_usdt']:.2f}</code> | "
                f"Strategy: <code>{p['strategy']}</code>\n"
                f"  Age: {p['age_min']:.0f}m | Conf: {p['confidence']:.2f}\n"
            )
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    async def _cmd_panic_close(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            return
        await update.message.reply_text("⚠️ <b>PANIC CLOSE</b> — Closing ALL positions...", parse_mode="HTML")

        if self._risk:
            await self._risk.emergency_pause("Manual panic close via Telegram")

        if self._close_all_callback:
            try:
                await self._close_all_callback("PANIC_CLOSE_TELEGRAM")
                await update.message.reply_text("✅ All positions closed. Trading paused.\nUse /resume to restart.")
            except Exception as e:
                await update.message.reply_text(f"❌ Error during close: {e}")
        else:
            await update.message.reply_text("⚠️ Close callback not registered.")

    async def _cmd_pause(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            return
        if self._risk:
            await self._risk.emergency_pause("Manual pause via Telegram")
        await update.message.reply_text("⏸ Trading paused. Use /resume to restart.")

    async def _cmd_resume(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            return
        if self._risk:
            await self._risk.resume_trading()
        await update.message.reply_text("▶️ Trading resumed.")

    async def _cmd_funding(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            return
        if not self._funding:
            await update.message.reply_text("Funding strategy not connected.")
            return

        summary = self._funding.get_funding_summary()
        if not summary:
            await update.message.reply_text("No funding data yet.")
            return

        lines = ["📈 <b>Funding Rates</b>\n"]
        for item in summary[:8]:
            status_icon = {
                "EXTREME_LONG": "🔴",
                "EXTREME_SHORT": "🟢",
                "NORMAL": "⚪",
            }.get(item["status"], "⚪")
            lines.append(
                f"{status_icon} <code>{item['symbol']}</code>: "
                f"{item['current_rate']:.4%} "
                f"({item['annualized_pct']:+.1f}% annualized)"
            )
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    async def _cmd_sentiment(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            return
        if not self._ai_brain:
            await update.message.reply_text("AI Brain not connected.")
            return

        s = self._ai_brain.get_sentiment()
        if not s:
            await update.message.reply_text("⏳ Sentiment not yet available.")
            return

        label_icon = {
            "VERY_BEARISH": "🔴🔴",
            "BEARISH": "🔴",
            "NEUTRAL": "⚪",
            "BULLISH": "🟢",
            "VERY_BULLISH": "🟢🟢",
        }.get(s.label, "⚪")

        bs_line = f"\n⚠️ <b>BLACK SWAN: {s.black_swan_reason}</b>" if s.black_swan else ""

        headlines = "\n".join(f"  • {h}" for h in s.key_headlines[:3])

        msg = (
            f"{label_icon} <b>AI Sentiment: {s.label}</b>\n\n"
            f"Score: <code>{s.score:+.1f}/10</code> | Confidence: {s.confidence:.0%}"
            f"{bs_line}\n\n"
            f"<b>Key Headlines:</b>\n{headlines}"
        )
        await update.message.reply_text(msg, parse_mode="HTML")

    # ─── NOTIFICATION HELPERS ─────────────────────────────────────────────────

    async def notify_trade_opened(self, order):
        icon = "🟢" if order.direction == "LONG" else "🔴"
        await self.send_message(
            f"{icon} <b>Trade Opened</b>\n"
            f"<code>{order.symbol}</code> {order.direction}\n"
            f"Entry: <code>{order.entry_price:.4f}</code>\n"
            f"SL: <code>{order.stop_loss:.4f}</code> | TP: <code>{order.take_profit:.4f}</code>\n"
            f"Risk: <code>${order.usdt_risk:.2f}</code> | Strategy: {order.strategy}\n"
            f"Confidence: {order.signal_confidence:.0%}"
        )

    async def notify_trade_closed(self, symbol: str, exit_price: float, pnl: float, reason: str):
        icon = "✅" if pnl >= 0 else "❌"
        await self.send_message(
            f"{icon} <b>Trade Closed: {symbol}</b>\n"
            f"Exit: <code>{exit_price:.4f}</code>\n"
            f"PnL: <code>${pnl:+.2f}</code>\n"
            f"Reason: {reason}"
        )

    async def notify_black_swan(self, reason: str):
        await self.send_message(
            f"⚠️ <b>BLACK SWAN DETECTED</b>\n\n"
            f"{reason}\n\n"
            f"All positions closed. Trading paused for {config.SENTIMENT_PAUSE_MINUTES} minutes."
        )
