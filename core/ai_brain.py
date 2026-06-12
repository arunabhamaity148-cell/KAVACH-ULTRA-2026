"""
KAVACH-ULTRA 2026 — core/ai_brain.py
GPT-4o-mini powered sentiment analysis and black swan detection.
Uses NewsAPI + RSS feeds + Twitter for news aggregation.
"""

import asyncio
import time
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Tuple
from datetime import datetime, timedelta

import aiohttp
import feedparser
from loguru import logger
from openai import AsyncOpenAI

import config


# ─── SENTIMENT DATA STRUCTURES ───────────────────────────────────────────────

@dataclass
class NewsItem:
    title: str
    source: str
    url: str
    published: float
    sentiment_score: Optional[float] = None


@dataclass
class SentimentResult:
    score: float
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


# ─── NEWS SCRAPERS ───────────────────────────────────────────────────────────

RSS_FEEDS = [
    "https://cointelegraph.com/rss",
    "https://coindesk.com/arc/outboundfeeds/rss/",
    "https://decrypt.co/feed",
    "https://cryptonews.com/news/feed/",
    "https://bitcoinmagazine.com/.rss/full/",
]


async def fetch_rss_headlines(session: aiohttp.ClientSession) -> List[NewsItem]:
    items: List[NewsItem] = []
    cutoff = time.time() - 3600

    async def _fetch_one(url: str):
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
                if r.status != 200:
                    return
                text = await r.text()
                feed = feedparser.parse(text)
                for entry in feed.entries[:10]:
                    pub = time.mktime(entry.get("published_parsed", time.gmtime()))
                    if pub < cutoff:
                        continue
                    items.append(NewsItem(
                        title=entry.get("title", ""),
                        source=url,
                        url=entry.get("link", ""),
                        published=pub,
                    ))
        except Exception as e:
            logger.debug(f"[AI BRAIN] RSS fetch error ({url}): {e}")

    await asyncio.gather(*[_fetch_one(u) for u in RSS_FEEDS])
    return items


async def fetch_newsapi_headlines(session: aiohttp.ClientSession) -> List[NewsItem]:
    if not config.NEWSAPI_KEY:
        logger.warning("[AI BRAIN] NEWSAPI_KEY not set. Skipping NewsAPI.")
        return []
    
    items: List[NewsItem] = []
    try:
        params = {
            "q": config.NEWSAPI_QUERY,
            "apiKey": config.NEWSAPI_KEY,
            "language": "en",
            "sortBy": "publishedAt",
            "pageSize": 20,
        }
        async with session.get(
            config.NEWSAPI_URL,
            params=params,
            timeout=aiohttp.ClientTimeout(total=10)
        ) as r:
            if r.status != 200:
                logger.warning(f"[AI BRAIN] NewsAPI error: {r.status}")
                return []
            
            data = await r.json()
            if data.get("status") != "ok":
                logger.warning(f"[AI BRAIN] NewsAPI response: {data.get('message', 'Unknown error')}")
                return []
            
            for article in data.get("articles", []):
                pub_str = article.get("publishedAt", "")
                try:
                    pub = datetime.fromisoformat(pub_str.replace("Z", "+00:00")).timestamp()
                except Exception:
                    pub = time.time()
                
                items.append(NewsItem(
                    title=article.get("title", ""),
                    source=article.get("source", {}).get("name", "newsapi"),
                    url=article.get("url", ""),
                    published=pub,
                ))
                
        logger.info(f"[AI BRAIN] NewsAPI fetched {len(items)} headlines")
        
    except Exception as e:
        logger.warning(f"[AI BRAIN] NewsAPI error: {e}")
    
    return items


async def fetch_cryptopanic_headlines(session: aiohttp.ClientSession) -> List[NewsItem]:
    if config.CRYPTOPANIC_API_KEY:
        logger.debug("[AI BRAIN] CryptoPanic deprecated. Use NewsAPI instead.")
    return []


async def fetch_twitter_mentions(session: aiohttp.ClientSession) -> List[NewsItem]:
    if not config.TWITTER_BEARER_TOKEN:
        return []
    items: List[NewsItem] = []
    query = (
        "(bitcoin OR ethereum OR crypto) "
        "(hack OR ban OR regulation OR crash OR pump OR squeeze) "
        "lang:en -is:retweet"
    )
    url = "https://api.twitter.com/2/tweets/search/recent"
    params = {
        "query": query,
        "max_results": 20,
        "tweet.fields": "created_at,public_metrics",
    }
    headers = {"Authorization": f"Bearer {config.TWITTER_BEARER_TOKEN}"}
    try:
        async with session.get(
            url, params=params, headers=headers,
            timeout=aiohttp.ClientTimeout(total=10)
        ) as r:
            if r.status != 200:
                return []
            data = await r.json()
            for tweet in data.get("data", []):
                metrics = tweet.get("public_metrics", {})
                engagement = metrics.get("retweet_count", 0) + metrics.get("like_count", 0)
                if engagement < 10:
                    continue
                items.append(NewsItem(
                    title=tweet.get("text", ""),
                    source="twitter",
                    url=f"https://twitter.com/i/web/status/{tweet['id']}",
                    published=time.time(),
                ))
    except Exception as e:
        logger.debug(f"[AI BRAIN] Twitter error: {e}")
    return items


# ─── GPT-4o-mini SENTIMENT ANALYZER ─────────────────────────────────────────

SENTIMENT_SYSTEM_PROMPT = """You are a crypto market sentiment analyst for an institutional trading bot.
Given a list of recent news headlines and tweets, you must:
1. Assess overall crypto market sentiment on a scale from -10 (extreme fear/bearish) to +10 (extreme greed/bullish).
2. Detect any BLACK SWAN events (exchange hack, regulatory ban, major fraud, war escalation, nuclear threat).
3. Return ONLY valid JSON, no other text.

JSON format:
{
  "score": <float -10 to 10>,
  "label": "<VERY_BEARISH|BEARISH|NEUTRAL|BULLISH|VERY_BULLISH>",
  "black_swan": <true|false>,
  "black_swan_reason": "<empty string or brief reason>",
  "key_headlines": ["<top 3 most impactful headlines>"],
  "confidence": <float 0 to 1>
}"""


class AIBrain:
    def __init__(self, db=None):
        self.client = AsyncOpenAI(api_key=config.OPENAI_API_KEY)
        self._session: Optional[aiohttp.ClientSession] = None
        self._current_sentiment: Optional[SentimentResult] = None
        self._blackswan_until: float = 0.0
        self._running = False
        self._lock = asyncio.Lock()
        self.db = db  # ✅ Database reference for logging

    async def start(self):
        self._running = True
        self._session = aiohttp.ClientSession()
        logger.info("[AI BRAIN] Starting sentiment monitor...")
        asyncio.create_task(self._monitor_loop())

    async def stop(self):
        self._running = False
        if self._session:
            await self._session.close()

    async def _monitor_loop(self):
        while self._running:
            try:
                await self._update_sentiment()
            except Exception as e:
                logger.error(f"[AI BRAIN] Monitor loop error: {e}")
            await asyncio.sleep(config.NEWS_REFRESH_SECONDS)

    async def _update_sentiment(self):
        rss_items, newsapi_items, tw_items = await asyncio.gather(
            fetch_rss_headlines(self._session),
            fetch_newsapi_headlines(self._session),
            fetch_twitter_mentions(self._session),
            return_exceptions=True,
        )

        all_items: List[NewsItem] = []
        for result in [rss_items, newsapi_items, tw_items]:
            if isinstance(result, list):
                all_items.extend(result)

        all_items.sort(key=lambda x: x.published, reverse=True)
        all_items = all_items[:30]

        if not all_items:
            logger.debug("[AI BRAIN] No headlines found this cycle")
            return

        headlines_text = "\n".join(
            f"- [{item.source}] {item.title}" for item in all_items
        )

        local_bs = self._local_blackswan_check(headlines_text)
        if local_bs:
            sentiment = SentimentResult(
                score=-10.0,
                label="VERY_BEARISH",
                black_swan=True,
                black_swan_reason=local_bs,
                key_headlines=[i.title for i in all_items[:3]],
                confidence=0.95,
                timestamp=time.time(),
            )
            await self._apply_sentiment(sentiment)
            return

        try:
            response = await self.client.chat.completions.create(
                model=config.AI_MODEL,
                max_tokens=500,
                temperature=0.1,
                messages=[
                    {"role": "system", "content": SENTIMENT_SYSTEM_PROMPT},
                    {"role": "user", "content": f"HEADLINES:\n{headlines_text}"},
                ],
            )
            raw = response.choices[0].message.content.strip()
            import json
            data = json.loads(raw)

            sentiment = SentimentResult(
                score=float(data.get("score", 0)),
                label=data.get("label", "NEUTRAL"),
                black_swan=bool(data.get("black_swan", False)),
                black_swan_reason=data.get("black_swan_reason", ""),
                key_headlines=data.get("key_headlines", []),
                confidence=float(data.get("confidence", 0.5)),
                timestamp=time.time(),
            )
            await self._apply_sentiment(sentiment)

        except Exception as e:
            logger.error(f"[AI BRAIN] GPT API error: {e}")

    def _local_blackswan_check(self, headlines_text: str) -> Optional[str]:
        text_lower = headlines_text.lower()
        for kw in config.SENTIMENT_BLACKSWAN_KEYWORDS:
            if kw in text_lower:
                return f"Keyword match: '{kw}'"
        return None

    async def _apply_sentiment(self, sentiment: SentimentResult):
        async with self._lock:
            self._current_sentiment = sentiment

        # ✅ LOG SENTIMENT TO DATABASE
        if self.db:
            try:
                await self.db.log_sentiment(sentiment)
                logger.debug("[AI BRAIN] Sentiment logged to database")
            except Exception as e:
                logger.warning(f"[AI BRAIN] Failed to log sentiment: {e}")

        if sentiment.black_swan:
            pause_until = time.time() + config.SENTIMENT_PAUSE_MINUTES * 60
            async with self._lock:
                self._blackswan_until = pause_until

            logger.critical(
                f"[AI BRAIN] ⚠️ BLACK SWAN: {sentiment.black_swan_reason} "
                f"| Trading paused {config.SENTIMENT_PAUSE_MINUTES}m"
            )
        else:
            logger.info(
                f"[AI BRAIN] Sentiment → {sentiment.label} "
                f"score={sentiment.score:+.1f} conf={sentiment.confidence:.2f}"
            )

    def get_sentiment(self) -> Optional[SentimentResult]:
        return self._current_sentiment

    def is_blackswan_active(self) -> bool:
        return time.time() < self._blackswan_until

    def get_blackswan_remaining_seconds(self) -> float:
        remaining = self._blackswan_until - time.time()
        return max(0.0, remaining)

    async def approve_signal(
        self,
        symbol: str,
        direction: str,
        base_confidence: float,
    ) -> SignalApproval:
        if self.is_blackswan_active():
            remaining = int(self.get_blackswan_remaining_seconds() / 60)
            return SignalApproval(
                approved=False,
                reason=f"BLACK SWAN active — paused ({remaining}m remaining)",
                sentiment_score=self._current_sentiment.score if self._current_sentiment else -10,
                confidence_boost=0.0,
            )

        if not self._current_sentiment:
            return SignalApproval(
                approved=False,
                reason="AI sentiment initializing — waiting for first fetch",
                sentiment_score=0.0,
                confidence_boost=0.0,
            )

        score = self._current_sentiment.score

        if direction == "LONG" and score < config.SENTIMENT_REJECT_THRESHOLD:
            return SignalApproval(
                approved=False,
                reason=f"LONG rejected: sentiment={score:.1f} < threshold={config.SENTIMENT_REJECT_THRESHOLD}",
                sentiment_score=score,
                confidence_boost=0.0,
            )

        if direction == "SHORT" and score > abs(config.SENTIMENT_REJECT_THRESHOLD):
            return SignalApproval(
                approved=False,
                reason=f"SHORT rejected: sentiment={score:.1f} > threshold={abs(config.SENTIMENT_REJECT_THRESHOLD)}",
                sentiment_score=score,
                confidence_boost=0.0,
            )

        confidence_boost = 0.0
        if direction == "LONG" and score > config.SENTIMENT_BOOST_THRESHOLD:
            confidence_boost = min((score - config.SENTIMENT_BOOST_THRESHOLD) / 10, 0.2)
        elif direction == "SHORT" and score < -config.SENTIMENT_BOOST_THRESHOLD:
            confidence_boost = min((abs(score) - config.SENTIMENT_BOOST_THRESHOLD) / 10, 0.2)

        return SignalApproval(
            approved=True,
            reason=f"AI approved | sentiment={score:+.1f} ({self._current_sentiment.label})",
            sentiment_score=score,
            confidence_boost=confidence_boost,
        )
