#!/usr/bin/env python3
"""
KAVACH-ULTRA 2026 — core/ai_brain.py  [v3 FIXED]
Fixes:
  - BUG #1: Black swan false positive loop (nuclear energy, cooldown, dedup)
  - BUG #2: approve_signal() missing is_arbitrage parameter
  - NewsAPI rate limit: fetch every 300s, not 60s
  - Multi-source fallback: RSS → NewsAPI
"""

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import List, Optional, Set

import aiohttp
import feedparser
from loguru import logger

try:
    from openai import AsyncOpenAI
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False

import config


# ─── DATA STRUCTURES ─────────────────────────────────────────────────────────

@dataclass
class SentimentResult:
    score: float           # -10 to +10
    label: str
    black_swan: bool
    black_swan_reason: str
    black_swan_severity: int   # 1=critical, 2=high, 3=medium
    key_headlines: List[str]
    confidence: float
    timestamp: float


@dataclass
class SignalApproval:
    approved: bool
    reason: str
    sentiment_score: float
    confidence_boost: float
    context_note: str = ""


# ─────────────────────────────────────────────────────────────────────────────
# FIX #1: BLACK SWAN — CONTEXTUAL MATCHING WITH WHITELIST + COOLDOWN
#
# OLD: any("nuclear" in h) → triggered on "nuclear energy" every 60s
# NEW: blacklist phrases + whitelist exclusions + 30min cooldown + dedup
# ─────────────────────────────────────────────────────────────────────────────

# Severity 1 — Pause 120 minutes (existential threats)
_BS_SEVERITY_1 = [
    "nuclear war", "nuclear attack", "nuclear strike", "nuclear explosion",
    "nuclear bomb", "nuclear crisis", "nuclear conflict", "nuclear threat",
    "nuclear warhead", "thermonuclear",
    "world war", "world war 3", "ww3",
    "exchange hacked", "exchange hack", "exchange exploited",
    "crypto exchange insolvent", "exchange bankrupt",
    "bitcoin banned globally", "crypto banned worldwide",
]

# Severity 2 — Pause 60 minutes (serious market threats)
_BS_SEVERITY_2 = [
    "market crash", "stock market crash", "crypto crash",
    "global recession", "financial crisis", "bank run",
    "stablecoin depeg", "usdt depeg", "usdc depeg", "dai depeg",
    "sec charges binance", "sec charges coinbase",
    "bitcoin etf rejected", "crypto exchange shutdown",
    "india crypto ban", "china crypto ban",
]

# Severity 3 — Pause 30 minutes (regulatory/moderate)
_BS_SEVERITY_3 = [
    "crypto regulation", "bitcoin regulation", "crypto legislation",
    "sec crypto", "cftc crypto", "fatf crypto",
    "exchange rug pull", "exit scam", "rug pull",
]

# Whitelist — these CANCEL any blacklist match in the same headline
_BS_WHITELIST = [
    "nuclear energy", "nuclear power plant", "nuclear power station",
    "nuclear medicine", "nuclear physics", "nuclear reactor",
    "nuclear family", "nuclear deal", "nuclear agreement",
    "nuclear fusion", "nuclear research", "nuclear science",
    "nuclear submarine" ,  # military but not direct threat
    "nuclear deterrent",   # strategic, not active threat
    "anti-nuclear", "denuclearization", "nuclear disarmament",
]

# Hard override — crypto-specific, always trigger regardless of whitelist
_BS_HARD_OVERRIDES = [
    "bitcoin exchange hacked",
    "crypto exchange hacked",
    "stablecoin depegged",
    "usdt lost peg",
    "ftx collapse",
]


def classify_blackswan(headline: str) -> tuple[bool, int, str]:
    """
    Returns (is_blackswan: bool, severity: int, reason: str)
    severity: 1=critical(120m), 2=high(60m), 3=medium(30m), 0=none
    """
    text = headline.lower()

    # Hard overrides — always trigger
    for phrase in _BS_HARD_OVERRIDES:
        if phrase in text:
            return True, 1, f"Hard override: '{phrase}'"

    # Check whitelist first — if whitelist match, cancel blacklist
    for white in _BS_WHITELIST:
        if white in text:
            return False, 0, ""

    # Severity 1
    for phrase in _BS_SEVERITY_1:
        if phrase in text:
            return True, 1, f"Critical threat: '{phrase}'"

    # Severity 2
    for phrase in _BS_SEVERITY_2:
        if phrase in text:
            return True, 2, f"Market threat: '{phrase}'"

    # Severity 3
    for phrase in _BS_SEVERITY_3:
        if phrase in text:
            return True, 3, f"Regulatory: '{phrase}'"

    return False, 0, ""


SEVERITY_PAUSE_MINUTES = {1: 120, 2: 60, 3: 30}


# ─── NEWS SOURCES ────────────────────────────────────────────────────────────

RSS_FEEDS = [
    "https://cointelegraph.com/rss",
    "https://coindesk.com/arc/outboundfeeds/rss/",
    "https://decrypt.co/feed",
    "https://cryptonews.com/news/feed/",
]

NEWSAPI_URL = "https://newsapi.org/v2/everything"


def _headline_id(title: str) -> str:
    """Unique ID for a headline — used for deduplication."""
    return hashlib.md5(title.lower().strip().encode()).hexdigest()[:12]


async def fetch_rss(session: aiohttp.ClientSession) -> List[str]:
    headlines = []
    cutoff = time.time() - 3600

    async def _one(url: str):
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
                if r.status != 200:
                    return
                feed = feedparser.parse(await r.text())
                for e in feed.entries[:8]:
                    pub = time.mktime(e.get("published_parsed") or time.gmtime())
                    if pub >= cutoff:
                        headlines.append(e.get("title", ""))
        except Exception as ex:
            logger.debug(f"[RSS] {url}: {ex}")

    await asyncio.gather(*[_one(u) for u in RSS_FEEDS])
    return [h for h in headlines if h]


async def fetch_newsapi(session: aiohttp.ClientSession, api_key: str) -> List[str]:
    if not api_key:
        return []
    try:
        params = {
            "q": "bitcoin OR ethereum OR crypto OR cryptocurrency",
            "sortBy": "publishedAt",
            "pageSize": 20,
            "apiKey": api_key,
        }
        async with session.get(
            NEWSAPI_URL, params=params,
            timeout=aiohttp.ClientTimeout(total=10)
        ) as r:
            if r.status != 200:
                logger.warning(f"[NEWSAPI] HTTP {r.status}")
                return []
            data = await r.json()
            return [a.get("title", "") for a in data.get("articles", []) if a.get("title")]
    except Exception as e:
        logger.warning(f"[NEWSAPI] Error: {e}")
        return []


# ─── GPT SENTIMENT ───────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """You are a crypto market sentiment analyst.
Given recent crypto news headlines, return ONLY valid JSON:
{
  "score": <float -10.0 to 10.0>,
  "label": "<VERY_BEARISH|BEARISH|NEUTRAL|BULLISH|VERY_BULLISH>",
  "key_headlines": ["<top 3 headlines>"],
  "confidence": <float 0.0 to 1.0>
}
No extra text."""


# ─── AI BRAIN ────────────────────────────────────────────────────────────────

class AIBrain:

    def __init__(self):
        api_key = getattr(config, "OPENAI_API_KEY", "")
        self._client = AsyncOpenAI(api_key=api_key) if OPENAI_AVAILABLE and api_key else None
        self._session: Optional[aiohttp.ClientSession] = None
        self._current: Optional[SentimentResult] = None
        self._running = False
        self._lock = asyncio.Lock()

        # FIX #1: Black swan cooldown — tracks when each severity was last fired
        self._bs_last_fired: dict = {}      # severity → timestamp
        self._bs_cooldown_sec = 1800        # 30 minutes between re-triggers

        # FIX #1: Headline deduplication — never re-process same headline
        self._seen_headline_ids: Set[str] = set()
        self._max_seen = 500                # cap memory usage

        # Pause tracker
        self._blackswan_until: float = 0.0
        self._blackswan_severity: int = 0

        # FIX: NewsAPI rate limit — fetch every 300s not 60s
        self._news_fetch_interval = 300     # 5 minutes

    async def start(self):
        self._running = True
        self._session = aiohttp.ClientSession()
        asyncio.create_task(self._monitor_loop())
        logger.info("[AI BRAIN] Sentiment monitor started (interval=300s)")

    async def stop(self):
        self._running = False
        if self._session:
            await self._session.close()

    async def _monitor_loop(self):
        while self._running:
            try:
                await self._update()
            except Exception as e:
                logger.error(f"[AI BRAIN] Loop error: {e}")
            await asyncio.sleep(self._news_fetch_interval)

    async def _update(self):
        # Fetch from RSS first (free, unlimited), NewsAPI as supplement
        newsapi_key = getattr(config, "NEWSAPI_KEY", "") or getattr(config, "CRYPTOPANIC_API_KEY", "")
        rss_headlines, api_headlines = await asyncio.gather(
            fetch_rss(self._session),
            fetch_newsapi(self._session, newsapi_key),
            return_exceptions=True,
        )

        all_headlines: List[str] = []
        for result in [rss_headlines, api_headlines]:
            if isinstance(result, list):
                all_headlines.extend(result)

        if not all_headlines:
            logger.debug("[AI BRAIN] No headlines this cycle")
            return

        # FIX #1: Filter out already-seen headlines
        new_headlines = []
        for h in all_headlines:
            hid = _headline_id(h)
            if hid not in self._seen_headline_ids:
                new_headlines.append(h)
                self._seen_headline_ids.add(hid)

        # Cap memory
        if len(self._seen_headline_ids) > self._max_seen:
            self._seen_headline_ids = set(list(self._seen_headline_ids)[-300:])

        if not new_headlines:
            logger.debug("[AI BRAIN] No new headlines (all already processed)")
            return

        logger.info(f"[AI BRAIN] Processing {len(new_headlines)} new headlines")

        # FIX #1: Check for black swan with severity levels
        for headline in new_headlines:
            is_bs, severity, reason = classify_blackswan(headline)
            if is_bs:
                await self._handle_blackswan(severity, reason, headline)
                break  # One trigger per cycle is enough

        # Get sentiment via GPT or fallback
        sentiment = await self._get_sentiment(new_headlines[:20])
        if sentiment:
            async with self._lock:
                self._current = sentiment
            logger.info(
                f"[AI BRAIN] Sentiment → {sentiment.label} "
                f"score={sentiment.score:+.1f} conf={sentiment.confidence:.2f}"
            )

    async def _handle_blackswan(self, severity: int, reason: str, headline: str):
        """
        FIX #1: Handle black swan with:
        - Per-severity cooldown (30 min between re-triggers)
        - Only pause if severity is higher than current pause
        - Log clearly
        """
        now = time.time()
        last_fired = self._bs_last_fired.get(severity, 0)

        # FIX #1: Cooldown check — don't re-trigger same severity within 30 min
        if now - last_fired < self._bs_cooldown_sec:
            remaining = int((self._bs_cooldown_sec - (now - last_fired)) / 60)
            logger.debug(
                f"[AI BRAIN] Black swan severity={severity} in cooldown "
                f"({remaining}m remaining) — skipping re-trigger"
            )
            return

        pause_min = SEVERITY_PAUSE_MINUTES.get(severity, 60)
        pause_until = now + pause_min * 60

        async with self._lock:
            # Only extend pause if new severity is >= current
            if severity <= self._blackswan_severity or now >= self._blackswan_until:
                self._blackswan_until = pause_until
                self._blackswan_severity = severity

        self._bs_last_fired[severity] = now

        logger.critical(
            f"[AI BRAIN] ⚠️ BLACK SWAN (severity={severity}): {reason} | "
            f"Headline: {headline[:80]} | Pausing {pause_min}m"
        )

    async def _get_sentiment(self, headlines: List[str]) -> Optional[SentimentResult]:
        text = "\n".join(f"- {h}" for h in headlines[:20])

        # Try GPT
        if self._client:
            try:
                resp = await self._client.chat.completions.create(
                    model=getattr(config, "AI_MODEL", "gpt-4o-mini"),
                    max_tokens=300,
                    temperature=0.1,
                    messages=[
                        {"role": "system", "content": _SYSTEM_PROMPT},
                        {"role": "user", "content": text},
                    ],
                )
                data = json.loads(resp.choices[0].message.content.strip())
                return SentimentResult(
                    score=float(data.get("score", 0)),
                    label=data.get("label", "NEUTRAL"),
                    black_swan=False,
                    black_swan_reason="",
                    black_swan_severity=0,
                    key_headlines=data.get("key_headlines", headlines[:3]),
                    confidence=float(data.get("confidence", 0.6)),
                    timestamp=time.time(),
                )
            except Exception as e:
                logger.warning(f"[AI BRAIN] GPT error: {e}")

        # Fallback: simple keyword scoring
        return self._simple_sentiment(headlines)

    def _simple_sentiment(self, headlines: List[str]) -> SentimentResult:
        """Fallback sentiment without GPT."""
        bullish_words = ["surge", "rally", "pump", "bull", "ath", "breakout", "gain", "rise", "up"]
        bearish_words = ["crash", "dump", "bear", "fall", "drop", "sell", "fear", "decline", "down"]

        score = 0.0
        for h in headlines:
            t = h.lower()
            score += sum(1 for w in bullish_words if w in t)
            score -= sum(1 for w in bearish_words if w in t)

        score = max(-10, min(10, score))
        if score > 3:
            label = "BULLISH"
        elif score > 0:
            label = "NEUTRAL"
        elif score > -3:
            label = "NEUTRAL"
        else:
            label = "BEARISH"

        return SentimentResult(
            score=round(score, 1),
            label=label,
            black_swan=False,
            black_swan_reason="",
            black_swan_severity=0,
            key_headlines=headlines[:3],
            confidence=0.5,
            timestamp=time.time(),
        )

    # ─── PUBLIC API ──────────────────────────────────────────────────────────

    def get_sentiment(self) -> Optional[SentimentResult]:
        return self._current

    def is_blackswan_active(self) -> bool:
        return time.time() < self._blackswan_until

    def blackswan_remaining_min(self) -> int:
        return max(0, int((self._blackswan_until - time.time()) / 60))

    # ─────────────────────────────────────────────────────────────────────────
    # FIX #2: approve_signal() — accepts is_arbitrage parameter
    # ─────────────────────────────────────────────────────────────────────────
    async def approve_signal(
        self,
        symbol: str,
        direction: str,
        confidence: float = 0.5,      # kept for backward compat
        metadata: dict = None,        # kept for backward compat
        is_arbitrage: bool = False,   # FIX #2: new parameter
    ) -> SignalApproval:
        """
        Gate every signal through AI approval.

        is_arbitrage=True: Lead-lag exchange arbitrage signals.
          - Sentiment mismatch is EXPECTED and EXPLAINED
          - Only blocked on extreme crash sentiment (< -8)

        is_arbitrage=False: Directional signals (sweep, OB, funding).
          - Normal sentiment filter applies
        """
        # Black swan hard block (all signal types)
        if self.is_blackswan_active():
            return SignalApproval(
                approved=False,
                reason=f"BLACK SWAN active — {self.blackswan_remaining_min()}m remaining "
                       f"(severity={self._blackswan_severity})",
                sentiment_score=self._current.score if self._current else -10,
                confidence_boost=0.0,
            )

        if not self._current:
            # Allow signals even without sentiment (don't block on startup)
            return SignalApproval(
                approved=True,
                reason="No sentiment data yet — allowing signal",
                sentiment_score=0.0,
                confidence_boost=0.0,
                context_note="⚠️ Sentiment not yet initialized",
            )

        score = self._current.score
        label = self._current.label

        # ── Arbitrage signal — loose sentiment filter ──────────────────────
        if is_arbitrage:
            ARBI_BLOCK = -8.0  # Only block on extreme market crash
            if direction == "LONG" and score < ARBI_BLOCK:
                return SignalApproval(
                    approved=False,
                    reason=f"Arbitrage LONG blocked: extreme crash sentiment ({score:.1f})",
                    sentiment_score=score,
                    confidence_boost=0.0,
                )
            if direction == "SHORT" and score > abs(ARBI_BLOCK):
                return SignalApproval(
                    approved=False,
                    reason=f"Arbitrage SHORT blocked: extreme euphoria ({score:.1f})",
                    sentiment_score=score,
                    confidence_boost=0.0,
                )

            # Explain mismatch if present
            if direction == "LONG" and score < 0:
                note = (
                    f"ℹ️ Sentiment {label} ({score:+.1f}) but this is an arbitrage "
                    f"signal — HL moved first, BN expected to follow. Not directional."
                )
            elif direction == "SHORT" and score > 0:
                note = (
                    f"ℹ️ Sentiment {label} ({score:+.1f}) but this is an arbitrage "
                    f"signal — HL dumped first, BN expected to follow. Not directional."
                )
            else:
                note = f"✅ Sentiment aligned: {label} ({score:+.1f})"

            return SignalApproval(
                approved=True,
                reason=f"Arbitrage signal approved | sentiment={score:+.1f}",
                sentiment_score=score,
                confidence_boost=0.0,
                context_note=note,
            )

        # ── Directional signal — normal sentiment filter ───────────────────
        reject_threshold = getattr(config, "SENTIMENT_REJECT_THRESHOLD", -5)

        if direction == "LONG" and score < reject_threshold:
            return SignalApproval(
                approved=False,
                reason=f"LONG rejected: sentiment={score:.1f} < {reject_threshold}",
                sentiment_score=score,
                confidence_boost=0.0,
            )
        if direction == "SHORT" and score > abs(reject_threshold):
            return SignalApproval(
                approved=False,
                reason=f"SHORT rejected: sentiment={score:.1f} > {abs(reject_threshold)}",
                sentiment_score=score,
                confidence_boost=0.0,
            )

        # Confidence boost for aligned sentiment
        boost = 0.0
        boost_threshold = getattr(config, "SENTIMENT_BOOST_THRESHOLD", 5)
        if direction == "LONG" and score > boost_threshold:
            boost = min((score - boost_threshold) / 10, 0.15)
        elif direction == "SHORT" and score < -boost_threshold:
            boost = min((abs(score) - boost_threshold) / 10, 0.15)

        return SignalApproval(
            approved=True,
            reason=f"Approved | {label} ({score:+.1f})",
            sentiment_score=score,
            confidence_boost=boost,
            context_note=f"✅ Sentiment: {label} ({score:+.1f})",
        )
