"""
KAVACH-ULTRA 2026 — signal_bot.py  [FIXED v2]
Signal-only mode: generates alerts → Telegram → manual execution.
Fixes applied:
  - BUG #2: Removed Rich Live widget (crashes Termius/mobile)
  - BUG #3: Confidence now 40–85% via multi-factor calc (in lead_lag.py)
  - BUG #4: Sentiment mismatch explained in Telegram message
  - BUG #5: Divergence shows real value (e.g. +0.1823%)
  - BUG #6: Black swan is crypto-contextual only
  - BUG #7: Cooldown tracked per-symbol with real timestamp
  - BUG #8: Telegram uses HTML parse mode
  - BUG #10: Paper mode / zero-balance no longer blocks signals
"""

import asyncio
import signal as signal_module
import sys
import time
from datetime import datetime
from typing import Optional

from loguru import logger

import config
from core.data_engine import DataEngine
from core.ai_brain import AIBrain
from utils.database import TradeDatabase
from utils.telegram_bot import TelegramController
from utils.lead_lag import LeadLagDetector, LeadLagSignal
from strategies.liquidity_sweep import LiquiditySweepStrategy
from strategies.order_flow import OrderFlowStrategy
from strategies.funding_squeeze import FundingSqueezeStrategy


# ─── LOGGING SETUP ───────────────────────────────────────────────────────────

logger.remove()
logger.add(
    sys.stderr,
    level=getattr(config, "LOG_LEVEL", "INFO"),
    format="<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | {message}",
    colorize=True,
)
logger.add(
    getattr(config, "LOG_FILE", "kavach_ultra.log"),
    rotation="50 MB",
    retention="14 days",
    level="DEBUG",
)


# ─── TELEGRAM MESSAGE BUILDER ────────────────────────────────────────────────
# FIX #8: HTML parse mode throughout (no Markdown — emojis break it)

def build_signal_message(
    signal: LeadLagSignal,
    sentiment_score: float,
    sentiment_label: str,
    context_note: str,
) -> str:
    """
    Build a clean, mobile-friendly Telegram signal message.
    FIX #4: includes context_note explaining sentiment vs direction.
    FIX #5: shows real divergence_pct value.
    FIX #3: shows realistic confidence.
    """
    icon       = "🟢" if signal.direction == "LONG" else "🔴"
    action     = "BUY / LONG" if signal.direction == "LONG" else "SELL / SHORT"
    ist_time   = datetime.now(config.IST).strftime("%H:%M:%S IST")
    vol_status = "✅ Confirmed" if signal.volume_confirmed else "⚠️ Unconfirmed"

    # FIX #5: divergence_pct is now the real value (e.g. +0.1823)
    div_str = f"{signal.divergence_pct:+.4f}%"

    # FIX #3: confidence shown as percentage with reality-check emoji
    conf_val = signal.confidence
    if conf_val >= 75:
        conf_str = f"{conf_val:.1f}% 🔥"
    elif conf_val >= 60:
        conf_str = f"{conf_val:.1f}% ✅"
    else:
        conf_str = f"{conf_val:.1f}% ⚠️"

    # Sentiment display
    label_map = {
        "VERY_BEARISH": "🔴🔴 Very Bearish",
        "BEARISH":       "🔴 Bearish",
        "NEUTRAL":       "⚪ Neutral",
        "BULLISH":       "🟢 Bullish",
        "VERY_BULLISH":  "🟢🟢 Very Bullish",
    }
    sentiment_display = label_map.get(sentiment_label, f"⚪ {sentiment_label}")

    msg = (
        f"{icon} <b>KAVACH-ULTRA SIGNAL</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📌 <b>Pair:</b> <code>{signal.symbol}</code>\n"
        f"📈 <b>Action:</b> <b>{action}</b>\n"
        f"\n"
        f"🔄 <b>Lead-Lag Data</b>\n"
        f"  HyperLiquid: <code>{signal.hl_price:.4f}</code>\n"
        f"  Binance:     <code>{signal.binance_price:.4f}</code>\n"
        f"  Divergence:  <code>{div_str}</code>\n"   # FIX #5
        f"  Volume:      {vol_status}\n"
        f"\n"
        f"🎯 <b>Confidence:</b> {conf_str}\n"       # FIX #3
        f"🧠 <b>Sentiment:</b> {sentiment_display} ({sentiment_score:+.1f})\n"
        f"\n"
    )

    # FIX #4: include explanation if sentiment and direction seem mismatched
    if context_note:
        msg += f"{context_note}\n\n"

    msg += (
        f"⏰ <b>Time:</b> {ist_time}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ <i>Manual execution on CoinDCX. Trade at your own risk.</i>"
    )

    return msg


def build_blackswan_message(reason: str, pause_min: int) -> str:
    return (
        f"⚠️ <b>BLACK SWAN DETECTED</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Reason:</b> {reason}\n\n"
        f"🔴 All new signals paused for <b>{pause_min} minutes</b>.\n"
        f"Use /resume to override manually."
    )


def build_status_message(bot: "SignalBot") -> str:
    """FIX #8: HTML format, no Markdown."""
    ist_time     = datetime.now(config.IST).strftime("%Y-%m-%d %H:%M:%S IST")
    sentiment    = bot.ai_brain.get_sentiment()
    bs_active    = bot.ai_brain.is_blackswan_active()
    bs_remaining = bot.ai_brain.blackswan_remaining_min()

    if sentiment:
        label_map = {
            "VERY_BEARISH": "🔴🔴", "BEARISH": "🔴",
            "NEUTRAL": "⚪", "BULLISH": "🟢", "VERY_BULLISH": "🟢🟢",
        }
        icon        = label_map.get(sentiment.label, "⚪")
        sent_str    = f"{icon} {sentiment.label} ({sentiment.score:+.1f})"
    else:
        sent_str = "⏳ Initializing..."

    pause_str = f"⛔ YES — {bs_remaining}m remaining" if bs_active else "✅ No"

    return (
        f"🛡 <b>KAVACH-ULTRA Status</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"🕐 {ist_time}\n"
        f"🧠 Sentiment: {sent_str}\n"
        f"⚠️ BlackSwan: {pause_str}\n"
        f"📊 Signals Today: {bot._signals_today} sent\n"
        f"🔄 Monitoring: {len(config.ACTIVE_PAIRS)} pairs\n"
        f"📡 Mode: Signal-Only (manual execution)"
    )


# ─── SIGNAL BOT ──────────────────────────────────────────────────────────────

class SignalBot:

    def __init__(self):
        self.db        = TradeDatabase()
        self.data_eng  = DataEngine()
        self.ai_brain  = AIBrain()
        self.telegram  = TelegramController()
        self.lead_lag  = LeadLagDetector()

        state = self.data_eng.state
        self.sweep   = LiquiditySweepStrategy(state)
        self.ob_flow = OrderFlowStrategy(state)
        self.funding = FundingSqueezeStrategy(state)

        self._running        = False
        self._signals_today  = 0
        # FIX #7: cooldown is now inside LeadLagDetector._last_signal_time
        # (removed from here to avoid double-tracking)

    async def start(self):
        self._running = True
        logger.info("🛡 KAVACH-ULTRA 2026 — Signal Bot starting...")

        await self.db.start()
        await self.ai_brain.start()

        # FIX #10: No risk manager dependency in signal-only mode
        # Removed: risk_manager calls that blocked signals at $0 balance

        # Register Telegram commands
        self.telegram.register_components(
            risk_manager=None,         # Not used in signal mode
            ai_brain=self.ai_brain,
            funding_strategy=self.funding,
            close_all_callback=None,
            status_callback=lambda: build_status_message(self),
        )
        await self.telegram.start()

        # Register price callback for lead-lag
        self.data_eng.register_callback(self._on_price_update)

        # Start data streams
        await self.data_eng.start()

        # Background tasks
        asyncio.create_task(self._candle_strategy_loop(), name="candle_strategies")
        asyncio.create_task(self._funding_monitor_loop(), name="funding_monitor")
        asyncio.create_task(self._heartbeat_loop(), name="heartbeat")

        await self.telegram.send_message(
            f"🛡 <b>KAVACH-ULTRA 2026 Started</b>\n"
            f"Mode: Signal-Only | Pairs: {len(config.ACTIVE_PAIRS)}\n"
            f"Time: {datetime.now(config.IST).strftime('%H:%M IST')}"
        )
        logger.success("✅ Signal Bot fully operational")

    async def stop(self):
        self._running = False
        logger.info("Shutting down...")
        await self.telegram.send_message("⚠️ KAVACH-ULTRA shutting down...")
        await self.data_eng.stop()
        await self.ai_brain.stop()
        await self.telegram.stop()
        await self.db.stop()
        logger.info("Stopped.")

    # ─── PRICE CALLBACK ──────────────────────────────────────────────────────

    async def _on_price_update(
        self,
        symbol: str,
        source: str,
        price: float,
        volume: float,
    ):
        """Called on every WebSocket price tick."""
        # FIX #7: cooldown is enforced inside lead_lag.on_price_update()
        signal = self.lead_lag.on_price_update(symbol, source, price, volume)
        if signal and signal.volume_confirmed:
            await self._process_lead_lag_signal(signal)

    # ─── LEAD-LAG SIGNAL PIPELINE ────────────────────────────────────────────

    async def _process_lead_lag_signal(self, signal: LeadLagSignal):
        """Full pipeline: AI approval → build message → send Telegram."""

        # Black swan guard
        if self.ai_brain.is_blackswan_active():
            logger.debug(f"[SIGNAL] {signal.symbol} blocked — black swan active")
            return

        # AI approval (arbitrage=True → sentiment mismatch allowed + explained)
        approval = await self.ai_brain.approve_signal(
            symbol=signal.symbol,
            direction=signal.direction,
            is_arbitrage=True,   # FIX #4: lead-lag is always arbitrage
        )

        await self.db.log_signal(
            symbol=signal.symbol,
            strategy="lead_lag",
            direction=signal.direction,
            confidence=signal.confidence,
            ai_approved=approval.approved,
            ai_reason=approval.reason,
            sentiment_score=approval.sentiment_score,
            entry_price=signal.binance_price,
            executed=False,
            reject_reason="" if approval.approved else approval.reason,
        )

        if not approval.approved:
            logger.info(f"[SIGNAL] {signal.symbol} rejected: {approval.reason}")
            return

        sentiment = self.ai_brain.get_sentiment()
        sent_score = sentiment.score if sentiment else 0.0
        sent_label = sentiment.label if sentiment else "NEUTRAL"

        # FIX #4 + #5 + #3: message now shows correct divergence, confidence, context
        msg = build_signal_message(
            signal=signal,
            sentiment_score=sent_score,
            sentiment_label=sent_label,
            context_note=approval.context_note,
        )

        # FIX #8: HTML parse mode
        await self.telegram.send_message(msg, parse_mode="HTML")
        self._signals_today += 1

        logger.success(
            f"✅ SIGNAL SENT: {signal.symbol} {signal.direction} | "
            f"Div={signal.divergence_pct:+.4f}% "
            f"Conf={signal.confidence:.1f}% "
            f"Vol={'✓' if signal.volume_confirmed else '✗'}"
        )

    # ─── CANDLE STRATEGY LOOP ────────────────────────────────────────────────

    async def _candle_strategy_loop(self):
        """Evaluate sweep and OB strategies every 30 seconds."""
        while self._running:
            try:
                for symbol in config.ACTIVE_PAIRS:
                    # Liquidity sweep
                    sweep_sig = self.sweep.evaluate(symbol)
                    if sweep_sig:
                        await self._process_non_arb_signal(
                            symbol=symbol,
                            direction=sweep_sig.direction,
                            strategy="sweep",
                            confidence=sweep_sig.confidence,
                            entry_price=sweep_sig.entry_price,
                            extra_info=(
                                f"Zone: {sweep_sig.zone_swept.zone_type} @ {sweep_sig.zone_swept.level:.4f}\n"
                                f"SL: {sweep_sig.stop_loss:.4f} | TP: {sweep_sig.take_profit:.4f}"
                            ),
                        )

                    # Order flow (only if confluence on both exchanges)
                    ob_sig = self.ob_flow.evaluate(symbol)
                    if ob_sig and ob_sig.confluence:
                        price = self.data_eng.state.get_price(symbol, "binance") or 0
                        await self._process_non_arb_signal(
                            symbol=symbol,
                            direction=ob_sig.direction,
                            strategy="order_flow",
                            confidence=ob_sig.confidence,
                            entry_price=price,
                            extra_info=(
                                f"Imbalance: {ob_sig.imbalance_ratio:.3f} | "
                                f"Spoof: {'Yes ⚠️' if ob_sig.spoofing_detected else 'No'}"
                            ),
                        )
            except Exception as e:
                logger.warning(f"[CANDLE LOOP] {e}")
            await asyncio.sleep(30)

    async def _funding_monitor_loop(self):
        """Check funding rates every 60 seconds."""
        while self._running:
            try:
                for symbol in config.ACTIVE_PAIRS:
                    fd = self.data_eng.state.funding.get(symbol)
                    if fd:
                        sig = self.funding.on_funding_update(fd)
                        if sig:
                            price = self.data_eng.state.get_price(symbol, "binance") or 0
                            await self._process_non_arb_signal(
                                symbol=symbol,
                                direction=sig.direction,
                                strategy="funding",
                                confidence=sig.confidence,
                                entry_price=price,
                                extra_info=(
                                    f"Funding: {sig.funding_rate:.4%} ({sig.squeeze_magnitude})\n"
                                    f"Consecutive extreme: {sig.consecutive_count} periods"
                                ),
                            )
            except Exception as e:
                logger.warning(f"[FUNDING LOOP] {e}")
            await asyncio.sleep(60)

    async def _process_non_arb_signal(
        self,
        symbol: str,
        direction: str,
        strategy: str,
        confidence: float,
        entry_price: float,
        extra_info: str,
    ):
        """Process sweep / OB / funding signals (not arbitrage — normal sentiment filter)."""

        if self.ai_brain.is_blackswan_active():
            return

        # FIX #7: simple cooldown for non-lead-lag signals too
        cooldown_key = f"{symbol}_{strategy}"
        last = getattr(self, "_non_arb_cooldown", {})
        if not hasattr(self, "_non_arb_cooldown"):
            self._non_arb_cooldown = {}
        elapsed = time.time() - self._non_arb_cooldown.get(cooldown_key, 0)
        if elapsed < 300:  # 5-minute cooldown
            return
        self._non_arb_cooldown[cooldown_key] = time.time()

        approval = await self.ai_brain.approve_signal(
            symbol=symbol,
            direction=direction,
            is_arbitrage=False,
        )

        if not approval.approved:
            return

        sentiment = self.ai_brain.get_sentiment()
        sent_score = sentiment.score if sentiment else 0.0
        sent_label = sentiment.label if sentiment else "NEUTRAL"

        icon   = "🟢" if direction == "LONG" else "🔴"
        action = "BUY / LONG" if direction == "LONG" else "SELL / SHORT"
        ist    = datetime.now(config.IST).strftime("%H:%M:%S IST")
        strat  = strategy.replace("_", " ").title()
        conf_str = f"{confidence * 100:.1f}%" if confidence <= 1 else f"{confidence:.1f}%"

        msg = (
            f"{icon} <b>KAVACH-ULTRA SIGNAL ({strat})</b>\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"📌 <b>Pair:</b> <code>{symbol}</code>\n"
            f"📈 <b>Action:</b> <b>{action}</b>\n"
            f"💰 <b>Entry ~</b> <code>{entry_price:.4f}</code>\n"
            f"\n"
            f"📊 {extra_info}\n"
            f"\n"
            f"🎯 <b>Confidence:</b> {conf_str}\n"
            f"🧠 <b>Sentiment:</b> {sent_label} ({sent_score:+.1f})\n"
            f"\n"
            f"{approval.context_note}\n"
            f"\n"
            f"⏰ <b>Time:</b> {ist}\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"⚠️ <i>Manual execution on CoinDCX. Trade at your own risk.</i>"
        )

        await self.telegram.send_message(msg, parse_mode="HTML")
        self._signals_today += 1

        logger.success(f"✅ SIGNAL SENT: {symbol} {direction} [{strategy}] Conf={conf_str}")

    # ─── HEARTBEAT ───────────────────────────────────────────────────────────

    async def _heartbeat_loop(self):
        """Log status every 30 minutes to confirm bot is alive."""
        while self._running:
            await asyncio.sleep(1800)
            logger.info(
                f"[HEARTBEAT] Running | Signals today: {self._signals_today} | "
                f"Time: {datetime.now(config.IST).strftime('%H:%M IST')}"
            )


# ─── ENTRY POINT ─────────────────────────────────────────────────────────────

async def main():
    bot = SignalBot()

    # Graceful shutdown on SIGTERM / SIGINT
    loop = asyncio.get_event_loop()

    def _shutdown(sig_name: str):
        logger.info(f"Received {sig_name}, shutting down...")
        asyncio.create_task(bot.stop())

    for sig in (signal_module.SIGTERM, signal_module.SIGINT):
        loop.add_signal_handler(sig, lambda s=sig.name: _shutdown(s))

    try:
        await bot.start()

        # FIX #2: NO Rich Live widget — simple sleep loop, safe on Termius/mobile
        logger.info("Bot running. Press Ctrl+C to stop.")
        while bot._running:
            await asyncio.sleep(60)

    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt received")
    except Exception as e:
        logger.critical(f"Fatal error: {e}", exc_info=True)
    finally:
        if bot._running:
            await bot.stop()


if __name__ == "__main__":
    asyncio.run(main())
