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

HYPERLIQUID_ADDRESS   = os.getenv("HYPERLIQUID_ADDRESS", "")
HYPERLIQUID_SECRET    = os.getenv("HYPERLIQUID_SECRET", "")

OPENAI_API_KEY        = os.getenv("OPENAI_API_KEY", "")

# ✅ NewsAPI (Replacing CryptoPanic)
NEWSAPI_KEY           = os.getenv("NEWSAPI_KEY", "")

# ❌ CryptoPanic (Deprecated — free plan discontinued)
CRYPTOPANIC_API_KEY   = os.getenv("CRYPTOPANIC_API_KEY", "")

TWITTER_BEARER_TOKEN  = os.getenv("TWITTER_BEARER_TOKEN", "")

TELEGRAM_BOT_TOKEN    = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID      = os.getenv("TELEGRAM_CHAT_ID", "")

# ─── CAPITAL & RISK ──────────────────────────────────────────────────────────
TOTAL_CAPITAL_USDT     = float(os.getenv("TOTAL_CAPITAL_USDT", "5000"))
DEFAULT_RISK_PCT       = 0.015
HIGH_CONF_RISK_PCT     = 0.030
DAILY_LOSS_LIMIT_PCT   = 0.05
MAX_OPEN_POSITIONS     = 3
LEVERAGE               = 10

# ─── TRADING UNIVERSE ────────────────────────────────────────────────────────
BANNED_PAIRS = {"XMRUSDT", "ZECUSDT", "DASHUSDT", "SCUSDT", "REPUSDT"}

ACTIVE_PAIRS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT",
    "AVAXUSDT", "LINKUSDT", "AAVEUSDT", "DOTUSDT",
    "MATICUSDT", "ARBUSDT", "OPUSDT", "INJUSDT",
]

HL_PAIR_MAP = {
    "BTCUSDT": "BTC", "ETHUSDT": "ETH", "SOLUSDT": "SOL",
    "BNBUSDT": "BNB", "AVAXUSDT": "AVAX", "LINKUSDT": "LINK",
    "AAVEUSDT": "AAVE", "DOTUSDT": "DOT", "MATICUSDT": "MATIC",
    "ARBUSDT": "ARB", "OPUSDT": "OP", "INJUSDT": "INJ",
}

# ─── TIME FILTER (IST) ───────────────────────────────────────────────────────
TRADING_START_HOUR = 9
TRADING_END_HOUR   = 0

# ─── LEAD-LAG DETECTOR ───────────────────────────────────────────────────────
LEAD_LAG_THRESHOLD_PCT   = 0.0015
LEAD_LAG_VOLUME_FACTOR   = 1.5
LEAD_LAG_WINDOW_SECONDS  = 30

# ─── ORDER BOOK / LIQUIDITY ──────────────────────────────────────────────────
OB_IMBALANCE_THRESHOLD   = 0.70
OB_SPOOFING_SIZE_MULT    = 5.0
OB_DEPTH_LEVELS          = 20
LIQUIDITY_WALL_MIN_USD   = 500_000

# ─── FUNDING RATE STRATEGY ───────────────────────────────────────────────────
FUNDING_EXTREME_THRESHOLD = 0.0008
FUNDING_SQUEEZE_WINDOW    = 3

# ─── LIQUIDITY SWEEP STRATEGY ────────────────────────────────────────────────
SWEEP_WICK_MULTIPLIER    = 1.5
SWEEP_REVERSAL_PCT       = 0.003
SWEEP_LOOKBACK_CANDLES   = 48
SWEEP_TIMEFRAME          = "15m"

# ─── AI SENTIMENT ────────────────────────────────────────────────────────────
SENTIMENT_REJECT_THRESHOLD    = -5
SENTIMENT_BOOST_THRESHOLD     = 5
SENTIMENT_BLACKSWAN_KEYWORDS  = [
    "exchange hack", "exchange hacked", "rug pull", "exit scam",
    "sec charges", "banned crypto", "india crypto ban",
    "cftc action", "emergency shutdown", "insolvency",
    "war escalation", "nuclear", "regulatory ban",
]
SENTIMENT_PAUSE_MINUTES  = 120
AI_MODEL                 = "gpt-4o-mini"
NEWS_REFRESH_SECONDS     = 60

# ─── NEWSAPI CONFIG ──────────────────────────────────────────────────────────
NEWSAPI_URL = "https://newsapi.org/v2/everything"
NEWSAPI_QUERY = "bitcoin OR ethereum OR crypto OR blockchain OR altcoin"

# ─── DATABASE ────────────────────────────────────────────────────────────────
DB_PATH = "kavach_ultra.db"

# ─── WEBSOCKET ───────────────────────────────────────────────────────────────
BINANCE_WS_BASE  = "wss://fstream.binance.com/stream"
BINANCE_REST     = "https://fapi.binance.com"
HL_WS_BASE       = "wss://api.hyperliquid.xyz/ws"
HL_REST          = "https://api.hyperliquid.xyz"

WS_RECONNECT_DELAY   = 5
WS_HEARTBEAT_TIMEOUT = 30

# ─── LOGGING ─────────────────────────────────────────────────────────────────
LOG_FILE   = "kavach_ultra.log"
LOG_LEVEL  = "INFO"
