"""
KAVACH-ULTRA 2026 — config.py
Master configuration. All secrets from .env, all constants here.
"""

import os
from dotenv import load_dotenv
from pytz import timezone

load_dotenv()

# ─── IDENTITY ────────────────────────────────────────────────────────────────
BOT_NAME = "KAVACH-ULTRA 2026"
VERSION  = "1.0.0"
IST      = timezone("Asia/Kolkata")

# ─── API KEYS ────────────────────────────────────────────────────────────────
BINANCE_API_KEY       = os.getenv("BINANCE_API_KEY", "")
BINANCE_API_SECRET    = os.getenv("BINANCE_API_SECRET", "")
BINANCE_TESTNET       = os.getenv("BINANCE_TESTNET", "true").lower() == "true"

HYPERLIQUID_ADDRESS   = os.getenv("HYPERLIQUID_ADDRESS", "")    # EVM wallet address
HYPERLIQUID_SECRET    = os.getenv("HYPERLIQUID_SECRET", "")     # Private key

OPENAI_API_KEY        = os.getenv("OPENAI_API_KEY", "")
CRYPTOPANIC_API_KEY   = os.getenv("CRYPTOPANIC_API_KEY", "")
TWITTER_BEARER_TOKEN  = os.getenv("TWITTER_BEARER_TOKEN", "")

TELEGRAM_BOT_TOKEN    = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID      = os.getenv("TELEGRAM_CHAT_ID", "")

# ─── CAPITAL & RISK ──────────────────────────────────────────────────────────
TOTAL_CAPITAL_USDT     = float(os.getenv("TOTAL_CAPITAL_USDT", "5000"))
DEFAULT_RISK_PCT       = 0.015   # 1.5% per trade
HIGH_CONF_RISK_PCT     = 0.030   # 3.0% for high-confidence AI signals
DAILY_LOSS_LIMIT_PCT   = 0.05    # 5% daily drawdown = hard stop
MAX_OPEN_POSITIONS     = 3
LEVERAGE               = 10      # Default leverage

# ─── TRADING UNIVERSE ────────────────────────────────────────────────────────
# Indian regulatory exclusions
BANNED_PAIRS = {"XMRUSDT", "ZECUSDT", "DASHUSDT", "SCUSDT", "REPUSDT"}

# Actively traded pairs (Binance Futures symbols)
ACTIVE_PAIRS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT",
    "AVAXUSDT", "LINKUSDT", "AAVEUSDT", "DOTUSDT",
    "MATICUSDT", "ARBUSDT", "OPUSDT", "INJUSDT",
]

# Corresponding Hyperliquid symbols (usually just base asset)
HL_PAIR_MAP = {
    "BTCUSDT": "BTC", "ETHUSDT": "ETH", "SOLUSDT": "SOL",
    "BNBUSDT": "BNB", "AVAXUSDT": "AVAX", "LINKUSDT": "LINK",
    "AAVEUSDT": "AAVE", "DOTUSDT": "DOT", "MATICUSDT": "MATIC",
    "ARBUSDT": "ARB", "OPUSDT": "OP", "INJUSDT": "INJ",
}

# ─── TIME FILTER (IST) ───────────────────────────────────────────────────────
TRADING_START_HOUR = 9    # 09:00 IST
TRADING_END_HOUR   = 0    # 00:00 IST (midnight)

# ─── LEAD-LAG DETECTOR ───────────────────────────────────────────────────────
LEAD_LAG_THRESHOLD_PCT   = 0.0015   # 0.15% price divergence
LEAD_LAG_VOLUME_FACTOR   = 1.5      # HL volume must be 1.5x its own avg
LEAD_LAG_WINDOW_SECONDS  = 30       # Rolling window for comparison

# ─── ORDER BOOK / LIQUIDITY ──────────────────────────────────────────────────
OB_IMBALANCE_THRESHOLD   = 0.70     # 70% bid/ask ratio → directional signal
OB_SPOOFING_SIZE_MULT    = 5.0      # Order 5x avg size = suspicious
OB_DEPTH_LEVELS          = 20       # Top 20 bid/ask levels
LIQUIDITY_WALL_MIN_USD   = 500_000  # $500k+ order = liquidity wall

# ─── FUNDING RATE STRATEGY ───────────────────────────────────────────────────
FUNDING_EXTREME_THRESHOLD = 0.0008  # 0.08% per 8h = extreme (annualised ~87%)
FUNDING_SQUEEZE_WINDOW    = 3       # 3 consecutive extreme readings

# ─── LIQUIDITY SWEEP STRATEGY ────────────────────────────────────────────────
SWEEP_WICK_MULTIPLIER    = 1.5      # Wick must be 1.5x avg candle body
SWEEP_REVERSAL_PCT       = 0.003    # 0.3% reversal from wick extreme
SWEEP_LOOKBACK_CANDLES   = 48       # Look back 48 candles for prev high/low
SWEEP_TIMEFRAME          = "15m"

# ─── AI SENTIMENT ────────────────────────────────────────────────────────────
SENTIMENT_REJECT_THRESHOLD    = -5    # Score < -5 → reject LONG signal
SENTIMENT_BOOST_THRESHOLD     = 5     # Score > +5 → boost confidence
SENTIMENT_BLACKSWAN_KEYWORDS  = [
    "exchange hack", "exchange hacked", "rug pull", "exit scam",
    "sec charges", "banned crypto", "india crypto ban",
    "cftc action", "emergency shutdown", "insolvency",
    "war escalation", "nuclear", "regulatory ban",
]
SENTIMENT_PAUSE_MINUTES  = 120       # Pause after black swan
AI_MODEL                 = "gpt-4o-mini"
NEWS_REFRESH_SECONDS     = 60        # Re-fetch news every 60s

# ─── DATABASE ────────────────────────────────────────────────────────────────
DB_PATH = "kavach_ultra.db"

# ─── WEBSOCKET ───────────────────────────────────────────────────────────────
BINANCE_WS_BASE  = "wss://fstream.binance.com/stream"
BINANCE_REST     = "https://fapi.binance.com"
HL_WS_BASE       = "wss://api.hyperliquid.xyz/ws"
HL_REST          = "https://api.hyperliquid.xyz"

WS_RECONNECT_DELAY   = 5     # seconds
WS_HEARTBEAT_TIMEOUT = 30    # seconds

# ─── LOGGING ─────────────────────────────────────────────────────────────────
LOG_FILE   = "kavach_ultra.log"
LOG_LEVEL  = "INFO"
