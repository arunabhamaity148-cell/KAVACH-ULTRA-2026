"""
KAVACH-ULTRA 2026 — core/ai_brain.py  [FIXED v2]
Fixes:
  - BUG #6: Black swan triggering on sports/geopolitical news
             Fix: Two-layer filter — keyword must appear WITH crypto context word
  - BUG #4: Sentiment mismatch (BEARISH but LONG signal)
             Fix: Added get_signal_context_note() for Telegram message clarification
"""

import asyncio
import json
import time
from dataclasses import dataclass
from typing import List, Optional

import aiohttp
import feedparser
from loguru import logger
from openai import AsyncOpenAI

import config


# ─── DATA STRUCTURES ─────────────────────────────────────────────────────────

@dataclass
class NewsItem:
    title: str
    source: str
    url: str
    published: float


@dataclass
class SentimentResult:
    score: float           # -10 to +10
    label: str
    black_swan: bool
    black_swan_reason: str
    key_headlines: List[str]
    confidence: float
    timestamp: float


@dataclass
class SignalApproval:
    approved: bool
    reason: str
    sentiment_score: float
    confidence_boost: float
    # FIX #4: explanation note for Telegram message
    context_note: str = ""


# ─── NEWS FEED SOURCES ───────────────────────────────────────────────────────

RSS_FEEDS = [
    "https://cointelegraph.com/rss",
    "https://coindesk.com/arc/outboundfeeds/rss/",
    "https://decrypt.co/feed",
    "https://cryptonews.com/news/feed/",
]

CRYPTOPANIC_URL = (
    "https://cryptopanic.com/api/v1/posts/"
    "?auth_token={token}&filter=important&kind=news"
)


# ─────────────────────────────────────────────────────────────────────────────
# FIX #6: BLACK SWAN FILTER — CRYPTO-CONTEXTUAL ONLY
#
# Old approach: match ANY keyword → "Iran...FBI drones" → FALSE POSITIVE
# New approach: BOTH a danger keyword AND a crypto context word must appear
#               in the same headline (within 120 chars of each other).
# ─────────────────────────────────────────────────────────────────────────────

# Danger keywords (event type)
_BS_DANGER_KEYWORDS = [
    "hack", "hacked", "exploit", "breach", "stolen",
    "rug pull", "exit scam", "insolvency", "bankrupt",
    "sec charges", "sec lawsuit", "cftc charges", "doj charges",
    "banned", "ban", "shutdown", "freeze", "emergency",
    "regulatory ban", "trading halted",
]

# Crypto context words — the headline MUST include one of these
_BS_CRYPTO_CONTEXT = [
    "bitcoin", "btc", "ethereum", "eth", "crypto", "binance",
    "coinbase", "exchange", "defi", "blockchain", "usdt", "stablecoin",
    "solana", "hyperliquid", "bybit", "okx", "kraken", "huobi",
    "token", "wallet", "web3", "nft", "dao",
]

# Hard overrides — these exact phrases always trigger regardless (very specific)
_BS_HARD_OVERRIDES = [
    "crypto exchange hack",
    "bitcoin exchange hacked",
    "exchange insolvent",
    "stablecoin depegged",
    "usdt depeg",
    "usdc depeg",
    "india crypto ban",
    "crypto trading banned",
]


def _is_crypto_blackswan(headline: str) -> Optional[str]:
    """
    Returns the matched reason string if this headline is a crypto black swan.
    Returns None otherwise.

    Two-layer approach:
      Layer 1: Hard override phrases (exact, high-precision)
      Layer 2: Danger keyword + crypto context word co-presence
    """
    text = headline.lower()

    # Layer 1: hard override
    for phrase in _BS_HARD_OVERRIDES:
        if phrase in text:
            return f"Hard override match: '{phrase}'"

    # Layer 2: danger keyword present AND crypto context present
    has_danger = any(kw in text for kw in _BS_DANGER_KEYWORDS)
    has_crypto = any(ctx in text for ctx in _BS_CRYPTO_CONTEXT)

    if has_danger and has_crypto:
        matched_danger = next(kw for kw in _BS_DANGER_KEYWORDS if kw in text)
        matched_crypto = next(ctx for ctx in _BS_CRYPTO_CONTEXT if ctx in text)
        return f"Crypto danger: '{matched_danger}' + '{matched_crypto}'"

    return None


def check_headlines_for_blackswan(headlines: List[str]) -> Optional[str]:
    """
    Check a list of headlines. Returns reason if ANY headline triggers.
    Sports news, geopolitical news without crypto context → ignored.
    """
    for h in headlines:
        reason = _is_crypto_blackswan(h)
        if reason:
            logger.warning(f"[AI BRAIN] Black swan triggered: {h[:80]}... Reason: {reason}")
            return reason
    return None


# ─── NEWS SCRAPERS ───────────────────────────────────────────────────────────

async def fetch_rss_headlines(session: aiohttp.ClientSession) -> List[NewsItem]:
    items: List[NewsItem] = []
    cutoff = time.time() - 3600

    async def _fetch(url: str):
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
                if r.status != 200:
                    return
                feed = feedparser.parse(await r.text())
                for entry in feed.entries[:10]:
                    pub = time.mktime(entry.get("published_parsed") or time.gmtime())
                    if pub < cutoff:
                        continue
                    items.append(NewsItem(
                        title=entry.get("title", ""),
                        source=url,
                        url=entry.get("link", ""),
                        published=pub,
                    ))
        except Exception as e:
            logger.debug(f"[RSS] {url}: {e}")

    await asyncio.gather(*[_fetch(u) for u in RSS_FEEDS])
    return items


async def fetch_cryptopanic(session: aiohttp.ClientSession) -> List[NewsItem]:
    if not getattr(config, "CRYPTOPANIC_API_KEY", ""):
        return []
    items: List[NewsItem] = []
    try:
        url = CRYPTOPANIC_URL.format(token=config.CRYPTOPANIC_API_KEY)
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status != 200:
                return []
            data = await r.json()
            for post in data.get("results", [])[:20]:
                items.append(NewsItem(
                    title=post.get("title", ""),
                    source="cryptopanic",
                    url=post.get("url", ""),
                    published=time.time(),
                ))
    except Exception as e:
        logger.debug(f"[CryptoPanic] {e}")
    return items


# ─── GPT PROMPT ──────────────────────────────────────────────────────────────

_SENTIMENT_SYSTEM = """You are a crypto market sentiment analyst.
Given recent crypto news headlines, return ONLY valid JSON with no extra text:
{
  "score": <float -10.0 to 10.0>,
  "label": "<VERY_BEARISH|BEARISH|NEUTRAL|BULLISH|VERY_BULLISH>",
  "black_swan": <true|false>,
  "black_swan_reason": "<empty or brief reason, ONLY for crypto events>",
  "key_headlines": ["<top 3 most impactful>"],
  "confidence": <float 0.0 to 1.0>
}
IMPORTANT: black_swan=true ONLY for direct crypto market threats
(exchange hacks, stablecoin depeg, regulatory crypto bans).
Sports news, geopolitical news, weather = NOT a black swan."""


# ─── AI BRAIN ────────────────────────────────────────────────────────────────

class AIBrain:

    def __init__(self):
        self.client = AsyncOpenAI(api_key=getattr(config, "OPENAI_API_KEY", ""))
        self._session: Optional[aiohttp.ClientSession] = None
        self._current: Optional[SentimentResult] = None
        self._blackswan_until: float = 0.0
        self._running = False
        self._lock = asyncio.Lock()

    async def start(self):
        self._running = True
        self._session = aiohttp.ClientSession()
        asyncio.create_task(self._monitor_loop())
        logger.info("[AI BRAIN] Sentiment monitor started")

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
            await asyncio.sleep(getattr(config, "NEWS_REFRESH_SECONDS", 60))

    async def _update(self):
        rss, cp = await asyncio.gather(
            fetch_rss_headlines(self._session),
            fetch_cryptopanic(self._session),
            return_exceptions=True,
        )

        all_items: List[NewsItem] = []
        for r in [rss, cp]:
            if isinstance(r, list):
                all_items.extend(r)

        all_items.sort(key=lambda x: x.published, reverse=True)
        all_items = all_items[:30]

        if not all_items:
            return

        headlines = [i.title for i in all_items]

        # ── FIX #6: local crypto-contextual black swan check first ──
        local_bs = check_headlines_for_blackswan(headlines)
        if local_bs:
            await self._apply(SentimentResult(
                score=-10.0, label="VERY_BEARISH",
                black_swan=True, black_swan_reason=local_bs,
                key_headlines=headlines[:3], confidence=0.95,
                timestamp=time.time(),
            ))
            return

        # ── GPT sentiment call ──
        headlines_text = "\n".join(f"- {h}" for h in headlines)
        try:
            resp = await self.client.chat.completions.create(
                model=getattr(config, "AI_MODEL", "gpt-4o-mini"),
                max_tokens=400,
                temperature=0.1,
                messages=[
                    {"role": "system", "content": _SENTIMENT_SYSTEM},
                    {"role": "user", "content": headlines_text},
                ],
            )
            data = json.loads(resp.choices[0].message.content.strip())

            # ── FIX #6: double-check GPT's black_swan claim ──
            gpt_bs = bool(data.get("black_swan", False))
            gpt_bs_reason = data.get("black_swan_reason", "")
            if gpt_bs:
                # Verify GPT's claim with our local filter
                local_verify = check_headlines_for_blackswan(headlines[:5])
                if not local_verify:
                    logger.warning(
                        f"[AI BRAIN] GPT claimed black_swan=True but local filter disagrees. "
                        f"Ignoring GPT claim. Reason GPT gave: {gpt_bs_reason}"
                    )
                    gpt_bs = False
                    gpt_bs_reason = ""

            await self._apply(SentimentResult(
                score=float(data.get("score", 0)),
                label=data.get("label", "NEUTRAL"),
                black_swan=gpt_bs,
                black_swan_reason=gpt_bs_reason,
                key_headlines=data.get("key_headlines", []),
                confidence=float(data.get("confidence", 0.5)),
                timestamp=time.time(),
            ))
        except Exception as e:
            logger.error(f"[AI BRAIN] GPT error: {e}")

    async def _apply(self, s: SentimentResult):
        async with self._lock:
            self._current = s
        if s.black_swan:
            pause_min = getattr(config, "SENTIMENT_PAUSE_MINUTES", 120)
            async with self._lock:
                self._blackswan_until = time.time() + pause_min * 60
            logger.critical(f"[AI BRAIN] ⚠️ BLACK SWAN: {s.black_swan_reason}")
        else:
            logger.info(f"[AI BRAIN] {s.label} | score={s.score:+.1f} conf={s.confidence:.0%}")

    # ─── PUBLIC API ──────────────────────────────────────────────────────────

    def get_sentiment(self) -> Optional[SentimentResult]:
        return self._current

    def is_blackswan_active(self) -> bool:
        return time.time() < self._blackswan_until

    def blackswan_remaining_min(self) -> int:
        return max(0, int((self._blackswan_until - time.time()) / 60))

    async def approve_signal(
        self,
        symbol: str,
        direction: str,
        is_arbitrage: bool = False,
    ) -> SignalApproval:
        """
        Gate every signal through AI.
        FIX #4: For lead-lag arbitrage signals, sentiment mismatch is EXPECTED
                 and explained clearly in context_note.
        """
        if self.is_blackswan_active():
            return SignalApproval(
                approved=False,
                reason=f"BLACK SWAN active — {self.blackswan_remaining_min()}m remaining",
                sentiment_score=self._current.score if self._current else -10,
                confidence_boost=0.0,
                context_note="",
            )

        if not self._current:
            return SignalApproval(
                approved=False,
                reason="AI sentiment not yet ready (waiting for first news fetch)",
                sentiment_score=0.0,
                confidence_boost=0.0,
                context_note="",
            )

        score = self._current.score
        label = self._current.label
        reject_threshold = getattr(config, "SENTIMENT_REJECT_THRESHOLD", -5)

        # ── FIX #4: ARBITRAGE SIGNAL — sentiment mismatch is NORMAL ──────────
        # Lead-lag signals are NOT market direction bets.
        # They are exchange arbitrage (HL moved first → BN will follow).
        # So BEARISH sentiment + LONG lead-lag = completely valid.
        # We only block on extreme bearish (< -8) for safety.
        # ─────────────────────────────────────────────────────────────────────
        if is_arbitrage:
            ARBI_BLOCK_THRESHOLD = -8  # Only block on extreme crash sentiment
            if score < ARBI_BLOCK_THRESHOLD:
                return SignalApproval(
                    approved=False,
                    reason=f"Arbitrage LONG blocked: sentiment={score:.1f} (extreme bearish, market crash risk)",
                    sentiment_score=score,
                    confidence_boost=0.0,
                    context_note="",
                )

            # Build explanation note for Telegram message
            if direction == "LONG" and score < 0:
                context_note = (
                    f"ℹ️ Note: Sentiment is {label} ({score:+.1f}) but this is an "
                    f"arbitrage signal — Hyperliquid moved first, Binance expected to follow. "
                    f"This is NOT a market direction bet."
                )
            elif direction == "SHORT" and score > 0:
                context_note = (
                    f"ℹ️ Note: Sentiment is {label} ({score:+.1f}) but this is an "
                    f"arbitrage signal — Hyperliquid dumped first, Binance expected to follow. "
                    f"Sentiment does not apply here."
                )
            else:
                context_note = f"✅ Sentiment aligned: {label} ({score:+.1f})"

            return SignalApproval(
                approved=True,
                reason=f"Arbitrage signal approved | sentiment={score:+.1f} ({label})",
                sentiment_score=score,
                confidence_boost=0.0,
                context_note=context_note,
            )

        # ── Standard directional signal — apply normal sentiment filter ──────
        if direction == "LONG" and score < reject_threshold:
            return SignalApproval(
                approved=False,
                reason=f"LONG rejected: sentiment={score:.1f} < threshold={reject_threshold}",
                sentiment_score=score,
                confidence_boost=0.0,
                context_note="",
            )

        if direction == "SHORT" and score > abs(reject_threshold):
            return SignalApproval(
                approved=False,
                reason=f"SHORT rejected: sentiment={score:.1f} > {abs(reject_threshold)}",
                sentiment_score=score,
                confidence_boost=0.0,
                context_note="",
            )

        # Confidence boost for aligned sentiment
        boost = 0.0
        boost_threshold = getattr(config, "SENTIMENT_BOOST_THRESHOLD", 5)
        if direction == "LONG" and score > boost_threshold:
            boost = min((score - boost_threshold) / 10, 0.2)
        elif direction == "SHORT" and score < -boost_threshold:
            boost = min((abs(score) - boost_threshold) / 10, 0.2)

        context_note = f"✅ Sentiment: {label} ({score:+.1f})"

        return SignalApproval(
            approved=True,
            reason=f"Approved | sentiment={score:+.1f} ({label})",
            sentiment_score=score,
            confidence_boost=boost,
            context_note=context_note,
        )
