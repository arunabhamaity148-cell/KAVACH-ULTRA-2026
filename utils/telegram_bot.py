"""
KAVACH-ULTRA 2026 — utils/telegram_bot.py  [FIXED v2]
Fixes:
  - BUG #8: All messages use parse_mode="HTML" (Markdown fails with emojis)
  - Added status_callback parameter for signal_bot.py integration
  - Removed risk_manager dependency (optional in signal-only mode)
"""

import asyncio
from typing import Optional, Callable, TYPE_CHECKING

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.error import TelegramError
from loguru import logger

import config

if TYPE_CHECKING:
    from core.ai_brain import AIBrain
    from strategies.funding_squeeze import FundingSqueezeStrategy


class TelegramController:

    def __init__(self):
        self._app: Optional[Application] = None
        self._risk          = None     # Optional — not used in signal mode
        self._ai_brain: Optional["AIBrain"] = None
        self._funding: Optional["FundingSqueezeStrategy"] = None
        self._close_all_cb: Optional[Callable] = None
        self._status_cb: Optional[Callable]    = None   # FIX: injectable status fn

    def register_components(
        self,
        risk_manager,           # Can be None in signal-only mode
        ai_brain: "AIBrain",
        funding_strategy: "FundingSqueezeStrategy",
        close_all_callback: Optional[Callable],
        status_callback: Optional[Callable] = None,
    ):
        self._risk         = risk_manager
        self._ai_brain     = ai_brain
        self._funding      = funding_strategy
        self._close_all_cb = close_all_callback
        self._status_cb    = status_callback

    async def start(self):
        token = getattr(config, "TELEGRAM_BOT_TOKEN", "")
        if not token:
            logger.warning("[TELEGRAM] No bot token. Telegram disabled.")
            return

        self._app = Application.builder().token(token).build()

        handlers = [
            ("start",     self._cmd_start),
            ("help",      self._cmd_help),
            ("status",    self._cmd_status),
            ("sentiment", self._cmd_sentiment),
            ("funding",   self._cmd_funding),
            ("resume",    self._cmd_resume),
            ("pause",     self._cmd_pause),
        ]
        for cmd, fn in handlers:
            self._app.add_handler(CommandHandler(cmd, fn))

        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling(drop_pending_updates=True)
        logger.success("[TELEGRAM] Bot connected ✓")

    async def stop(self):
        if self._app:
            try:
                await self._app.updater.stop()
                await self._app.stop()
                await self._app.shutdown()
            except Exception as e:
                logger.warning(f"[TELEGRAM] Stop error: {e}")

    def _authorized(self, update: Update) -> bool:
        return str(update.effective_chat.id) == str(getattr(config, "TELEGRAM_CHAT_ID", ""))

    # FIX #8: ALL reply calls use parse_mode="HTML"
    async def _reply(self, update: Update, text: str):
        try:
            await update.message.reply_text(text, parse_mode="HTML")
        except TelegramError as e:
            logger.warning(f"[TELEGRAM] Reply error: {e}")
            # Fallback: strip formatting and send plain
            plain = text.replace("<b>", "").replace("</b>", "") \
                        .replace("<code>", "").replace("</code>", "") \
                        .replace("<i>", "").replace("</i>", "")
            try:
                await update.message.reply_text(plain)
            except Exception:
                pass

    # ─── COMMANDS ────────────────────────────────────────────────────────────

    async def _cmd_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._authorized(update):
            return
        await self._reply(update,
            "🛡 <b>KAVACH-ULTRA 2026</b>\n"
            "Signal bot is running.\n"
            "Use /help for all commands."
        )

    async def _cmd_help(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._authorized(update):
            return
        await self._reply(update,
            "🛡 <b>KAVACH-ULTRA Commands</b>\n\n"
            "/status — Bot status and signal count\n"
            "/sentiment — Current AI sentiment score\n"
            "/funding — Top funding rate pairs\n"
            "/pause — Pause new signals\n"
            "/resume — Resume signals\n"
        )

    async def _cmd_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._authorized(update):
            return
        if self._status_cb:
            msg = self._status_cb()
        else:
            msg = "🛡 <b>KAVACH-ULTRA</b> — Running (no detailed status available)"
        await self._reply(update, msg)

    async def _cmd_sentiment(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._authorized(update):
            return
        if not self._ai_brain:
            await self._reply(update, "AI Brain not connected.")
            return

        s = self._ai_brain.get_sentiment()
        if not s:
            await self._reply(update, "⏳ Sentiment not yet initialized.")
            return

        label_icons = {
            "VERY_BEARISH": "🔴🔴", "BEARISH": "🔴",
            "NEUTRAL": "⚪", "BULLISH": "🟢", "VERY_BULLISH": "🟢🟢",
        }
        icon = label_icons.get(s.label, "⚪")
        bs   = f"\n⚠️ <b>BLACK SWAN ACTIVE</b>\n{s.black_swan_reason}" if s.black_swan else ""

        headlines = "\n".join(f"  • {h[:60]}" for h in s.key_headlines[:3])

        await self._reply(update,
            f"{icon} <b>AI Sentiment: {s.label}</b>\n\n"
            f"Score: <code>{s.score:+.1f} / 10</code>\n"
            f"Confidence: {s.confidence:.0%}"
            f"{bs}\n\n"
            f"<b>Key Headlines:</b>\n{headlines}"
        )

    async def _cmd_funding(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._authorized(update):
            return
        if not self._funding:
            await self._reply(update, "Funding data not available.")
            return

        summary = self._funding.get_funding_summary()
        if not summary:
            await self._reply(update, "No funding data yet.")
            return

        lines = ["📈 <b>Funding Rates (Top 8)</b>\n"]
        for item in summary[:8]:
            icon = {"EXTREME_LONG": "🔴", "EXTREME_SHORT": "🟢", "NORMAL": "⚪"}.get(
                item["status"], "⚪"
            )
            lines.append(
                f"{icon} <code>{item['symbol']}</code>: "
                f"{item['current_rate']:.4%} "
                f"({item['annualized_pct']:+.1f}% ann)"
            )
        await self._reply(update, "\n".join(lines))

    async def _cmd_pause(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._authorized(update):
            return
        if self._risk:
            await self._risk.emergency_pause("Manual pause via Telegram")
        await self._reply(update, "⏸ Signals paused. Use /resume to restart.")

    async def _cmd_resume(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._authorized(update):
            return
        if self._risk:
            await self._risk.resume_trading()
        await self._reply(update, "▶️ Signals resumed.")

    # ─── SEND HELPER ─────────────────────────────────────────────────────────

    async def send_message(self, text: str, parse_mode: str = "HTML"):
        """FIX #8: default parse_mode is HTML, not Markdown."""
        if not self._app:
            return
        chat_id = getattr(config, "TELEGRAM_CHAT_ID", "")
        if not chat_id:
            return
        try:
            await self._app.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=parse_mode,
            )
        except TelegramError as e:
            logger.warning(f"[TELEGRAM] send_message error: {e}")
            # Fallback: try without parse_mode
            try:
                plain = text.replace("<b>", "").replace("</b>", "") \
                            .replace("<code>", "").replace("</code>", "") \
                            .replace("<i>", "").replace("</i>", "")
                await self._app.bot.send_message(chat_id=chat_id, text=plain)
            except Exception:
                pass
