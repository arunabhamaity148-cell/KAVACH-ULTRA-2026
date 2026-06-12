"""
KAVACH-ULTRA 2026 — core/data_engine.py
Dual WebSocket ingestion: Binance Futures + Hyperliquid DEX.
Maintains shared state: prices, order books, trades, funding rates.
"""

import asyncio
import json
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Callable, Any

import aiohttp
import websockets
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential

import config


# ─── DATA STRUCTURES ─────────────────────────────────────────────────────────

@dataclass
class PriceBar:
    symbol: str
    timestamp: float
    open: float
    high: float
    low: float
    close: float
    volume: float
    source: str  # "binance" | "hyperliquid"


@dataclass
class OrderBookLevel:
    price: float
    qty: float


@dataclass
class OrderBook:
    symbol: str
    source: str
    bids: List[OrderBookLevel] = field(default_factory=list)  # sorted desc
    asks: List[OrderBookLevel] = field(default_factory=list)  # sorted asc
    timestamp: float = 0.0

    @property
    def best_bid(self) -> float:
        return self.bids[0].price if self.bids else 0.0

    @property
    def best_ask(self) -> float:
        return self.asks[0].price if self.asks else 0.0

    @property
    def mid_price(self) -> float:
        return (self.best_bid + self.best_ask) / 2 if self.bids and self.asks else 0.0

    @property
    def bid_total_usd(self) -> float:
        return sum(l.price * l.qty for l in self.bids)

    @property
    def ask_total_usd(self) -> float:
        return sum(l.price * l.qty for l in self.asks)

    @property
    def imbalance(self) -> float:
        """Returns bid/(bid+ask) ratio. >0.7 = bid heavy, <0.3 = ask heavy."""
        total = self.bid_total_usd + self.ask_total_usd
        return self.bid_total_usd / total if total > 0 else 0.5

    def get_liquidity_walls(self) -> List[Dict]:
        """Find orders >= LIQUIDITY_WALL_MIN_USD on either side."""
        walls = []
        for level in self.bids:
            usd_val = level.price * level.qty
            if usd_val >= config.LIQUIDITY_WALL_MIN_USD:
                walls.append({"side": "bid", "price": level.price, "usd": usd_val})
        for level in self.asks:
            usd_val = level.price * level.qty
            if usd_val >= config.LIQUIDITY_WALL_MIN_USD:
                walls.append({"side": "ask", "price": level.price, "usd": usd_val})
        return sorted(walls, key=lambda x: x["usd"], reverse=True)

    def detect_spoofing(self) -> List[Dict]:
        """Flag orders that are SPOOF_SIZE_MULT times the average size."""
        all_levels = self.bids + self.asks
        if not all_levels:
            return []
        avg_qty = sum(l.qty for l in all_levels) / len(all_levels)
        threshold = avg_qty * config.OB_SPOOFING_SIZE_MULT
        spoof_candidates = []
        for level in all_levels:
            if level.qty >= threshold:
                side = "bid" if level in self.bids else "ask"
                spoof_candidates.append({
                    "side": side,
                    "price": level.price,
                    "qty": level.qty,
                    "multiple_of_avg": round(level.qty / avg_qty, 1),
                })
        return spoof_candidates


@dataclass
class FundingData:
    symbol: str
    rate: float
    predicted_rate: float
    next_funding_time: float
    timestamp: float


# ─── SHARED MARKET STATE ─────────────────────────────────────────────────────

class MarketState:
    """Thread-safe (asyncio) shared state for all market data."""

    def __init__(self):
        # Latest prices: {"BTCUSDT": {"binance": 65000.0, "hyperliquid": 65005.0}}
        self.prices: Dict[str, Dict[str, float]] = defaultdict(dict)

        # Order books
        self.order_books: Dict[str, Dict[str, OrderBook]] = defaultdict(dict)

        # Candle history (rolling 200 candles per pair)
        self.candles: Dict[str, deque] = defaultdict(lambda: deque(maxlen=200))

        # Recent trades (rolling 100 trades per pair per source)
        self.trades: Dict[str, Dict[str, deque]] = defaultdict(
            lambda: defaultdict(lambda: deque(maxlen=100))
        )

        # Funding rates
        self.funding: Dict[str, FundingData] = {}

        # Volume baselines (rolling 20-period avg per pair per source)
        self.vol_baselines: Dict[str, Dict[str, deque]] = defaultdict(
            lambda: defaultdict(lambda: deque(maxlen=20))
        )

        # Callbacks registered by strategies
        self._callbacks: List[Callable] = []

        # Last update timestamps
        self.last_update: Dict[str, float] = defaultdict(float)

        self._lock = asyncio.Lock()

    def register_callback(self, fn: Callable):
        self._callbacks.append(fn)

    async def update_price(self, symbol: str, source: str, price: float, volume: float = 0.0):
        async with self._lock:
            self.prices[symbol][source] = price
            self.vol_baselines[symbol][source].append(volume)
            self.last_update[f"{symbol}_{source}"] = time.time()

        # Fire callbacks (outside lock to avoid deadlocks)
        for cb in self._callbacks:
            try:
                await cb(symbol, source, price, volume)
            except Exception as e:
                logger.warning(f"Callback error: {e}")

    async def update_order_book(self, symbol: str, source: str, ob: OrderBook):
        async with self._lock:
            self.order_books[symbol][source] = ob

    async def update_funding(self, fd: FundingData):
        async with self._lock:
            self.funding[fd.symbol] = fd

    def get_price(self, symbol: str, source: str) -> Optional[float]:
        return self.prices.get(symbol, {}).get(source)

    def get_order_book(self, symbol: str, source: str) -> Optional[OrderBook]:
        return self.order_books.get(symbol, {}).get(source)

    def get_avg_volume(self, symbol: str, source: str) -> float:
        dq = self.vol_baselines.get(symbol, {}).get(source)
        if not dq or len(dq) == 0:
            return 0.0
        return sum(dq) / len(dq)


# ─── BINANCE FUTURES DATA ENGINE ─────────────────────────────────────────────

class BinanceDataEngine:

    def __init__(self, state: MarketState, pairs: List[str]):
        self.state = state
        self.pairs = [p for p in pairs if p not in config.BANNED_PAIRS]
        self._running = False
        self._session: Optional[aiohttp.ClientSession] = None

    async def start(self):
        self._running = True
        self._session = aiohttp.ClientSession()
        logger.info(f"[BINANCE] Starting data engine for {len(self.pairs)} pairs")
        await asyncio.gather(
            self._stream_book_tickers(),
            self._stream_order_books(),
            self._poll_funding_rates(),
            self._stream_klines(),
        )

    async def stop(self):
        self._running = False
        if self._session:
            await self._session.close()

    # ── Ticker stream (price + volume) ──
    async def _stream_book_tickers(self):
        streams = "/".join(f"{p.lower()}@bookTicker" for p in self.pairs)
        url = f"{config.BINANCE_WS_BASE}?streams={streams}"
        await self._ws_connect_loop(url, self._handle_book_ticker, "book_ticker")

    async def _handle_book_ticker(self, msg: dict):
        if "data" not in msg:
            return
        d = msg["data"]
        symbol = d.get("s", "")
        if not symbol:
            return
        bid = float(d.get("b", 0))
        ask = float(d.get("a", 0))
        mid = (bid + ask) / 2
        if mid > 0:
            await self.state.update_price(symbol, "binance", mid)

    # ── Order book depth stream ──
    async def _stream_order_books(self):
        streams = "/".join(f"{p.lower()}@depth{config.OB_DEPTH_LEVELS}@500ms" for p in self.pairs)
        url = f"{config.BINANCE_WS_BASE}?streams={streams}"
        await self._ws_connect_loop(url, self._handle_depth, "depth")

    async def _handle_depth(self, msg: dict):
        if "data" not in msg:
            return
        d = msg["data"]
        symbol = d.get("s", "")
        if not symbol:
            return

        bids = [OrderBookLevel(float(b[0]), float(b[1])) for b in d.get("b", [])]
        asks = [OrderBookLevel(float(a[0]), float(a[1])) for a in d.get("a", [])]
        bids.sort(key=lambda x: x.price, reverse=True)
        asks.sort(key=lambda x: x.price)

        # Remove zero-quantity levels (Binance sends these to indicate removal)
        bids = [b for b in bids if b.qty > 0]
        asks = [a for a in asks if a.qty > 0]

        ob = OrderBook(
            symbol=symbol, source="binance",
            bids=bids[:config.OB_DEPTH_LEVELS],
            asks=asks[:config.OB_DEPTH_LEVELS],
            timestamp=time.time(),
        )
        await self.state.update_order_book(symbol, "binance", ob)

    # ── Kline stream for candle data ──
    async def _stream_klines(self):
        streams = "/".join(
            f"{p.lower()}@kline_{config.SWEEP_TIMEFRAME}" for p in self.pairs
        )
        url = f"{config.BINANCE_WS_BASE}?streams={streams}"
        await self._ws_connect_loop(url, self._handle_kline, "kline")

    async def _handle_kline(self, msg: dict):
        if "data" not in msg:
            return
        d = msg["data"]
        k = d.get("k", {})
        if not k.get("x", False):  # Only closed candles
            return
        bar = PriceBar(
            symbol=d.get("s", ""),
            timestamp=float(k["T"]) / 1000,
            open=float(k["o"]),
            high=float(k["h"]),
            low=float(k["l"]),
            close=float(k["c"]),
            volume=float(k["v"]),
            source="binance",
        )
        self.state.candles[bar.symbol].append(bar)
        # Also update price + volume from closed candle
        await self.state.update_price(bar.symbol, "binance", bar.close, bar.volume)

    # ── Funding rate polling (REST) ──
    async def _poll_funding_rates(self):
        while self._running:
            try:
                for symbol in self.pairs:
                    url = f"{config.BINANCE_REST}/fapi/v1/premiumIndex?symbol={symbol}"
                    async with self._session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                        if r.status == 200:
                            data = await r.json()
                            fd = FundingData(
                                symbol=symbol,
                                rate=float(data.get("lastFundingRate", 0)),
                                predicted_rate=float(data.get("interestRate", 0)),
                                next_funding_time=float(data.get("nextFundingTime", 0)) / 1000,
                                timestamp=time.time(),
                            )
                            await self.state.update_funding(fd)
            except Exception as e:
                logger.warning(f"[BINANCE] Funding rate poll error: {e}")
            await asyncio.sleep(300)  # Every 5 minutes

    # ── Generic WebSocket connection loop with auto-reconnect ──
    async def _ws_connect_loop(self, url: str, handler: Callable, stream_name: str):
        while self._running:
            try:
                logger.info(f"[BINANCE] Connecting to {stream_name} stream...")
                async with websockets.connect(
                    url,
                    ping_interval=20,
                    ping_timeout=config.WS_HEARTBEAT_TIMEOUT,
                    close_timeout=10,
                ) as ws:
                    logger.success(f"[BINANCE] {stream_name} connected ✓")
                    async for raw in ws:
                        if not self._running:
                            break
                        try:
                            msg = json.loads(raw)
                            await handler(msg)
                        except Exception as e:
                            logger.warning(f"[BINANCE] {stream_name} handler error: {e}")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[BINANCE] {stream_name} WS error: {e}. Reconnecting in {config.WS_RECONNECT_DELAY}s...")
                await asyncio.sleep(config.WS_RECONNECT_DELAY)


# ─── HYPERLIQUID DATA ENGINE ──────────────────────────────────────────────────

class HyperliquidDataEngine:

    def __init__(self, state: MarketState, pairs: List[str]):
        self.state = state
        # Map Binance symbols to HL coins
        self.hl_coins = [
            config.HL_PAIR_MAP[p] for p in pairs
            if p in config.HL_PAIR_MAP and p not in config.BANNED_PAIRS
        ]
        self.binance_symbol_map = {
            v: k for k, v in config.HL_PAIR_MAP.items()
        }
        self._running = False
        self._ws: Any = None

    async def start(self):
        self._running = True
        logger.info(f"[HYPERLIQUID] Starting data engine for {len(self.hl_coins)} coins")
        await asyncio.gather(
            self._stream_trades(),
            self._stream_order_book(),
        )

    async def stop(self):
        self._running = False

    async def _stream_trades(self):
        """Subscribe to real-time trade feed for all coins."""
        while self._running:
            try:
                async with websockets.connect(
                    config.HL_WS_BASE,
                    ping_interval=20,
                    ping_timeout=config.WS_HEARTBEAT_TIMEOUT,
                ) as ws:
                    # Subscribe to all coins
                    for coin in self.hl_coins:
                        sub_msg = json.dumps({
                            "method": "subscribe",
                            "subscription": {"type": "trades", "coin": coin}
                        })
                        await ws.send(sub_msg)

                    logger.success("[HYPERLIQUID] Trade stream connected ✓")

                    async for raw in ws:
                        if not self._running:
                            break
                        try:
                            msg = json.loads(raw)
                            await self._handle_trade(msg)
                        except Exception as e:
                            logger.warning(f"[HL] Trade handler error: {e}")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[HL] Trade WS error: {e}. Reconnecting...")
                await asyncio.sleep(config.WS_RECONNECT_DELAY)

    async def _handle_trade(self, msg: dict):
        if msg.get("channel") != "trades":
            return
        data = msg.get("data", [])
        if not isinstance(data, list):
            return

        for trade in data:
            coin = trade.get("coin", "")
            binance_sym = self.binance_symbol_map.get(coin)
            if not binance_sym:
                continue
            price = float(trade.get("px", 0))
            qty   = float(trade.get("sz", 0))
            if price > 0:
                await self.state.update_price(binance_sym, "hyperliquid", price, qty)
                self.state.trades[binance_sym]["hyperliquid"].append({
                    "price": price, "qty": qty, "ts": trade.get("time", time.time() * 1000)
                })

    async def _stream_order_book(self):
        """Subscribe to L2 book for all coins."""
        while self._running:
            try:
                async with websockets.connect(config.HL_WS_BASE) as ws:
                    for coin in self.hl_coins:
                        sub_msg = json.dumps({
                            "method": "subscribe",
                            "subscription": {"type": "l2Book", "coin": coin}
                        })
                        await ws.send(sub_msg)

                    logger.success("[HYPERLIQUID] Order book stream connected ✓")

                    async for raw in ws:
                        if not self._running:
                            break
                        try:
                            msg = json.loads(raw)
                            await self._handle_l2book(msg)
                        except Exception as e:
                            logger.warning(f"[HL] L2 handler error: {e}")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[HL] L2 WS error: {e}. Reconnecting...")
                await asyncio.sleep(config.WS_RECONNECT_DELAY)

    async def _handle_l2book(self, msg: dict):
        if msg.get("channel") != "l2Book":
            return
        data = msg.get("data", {})
        coin = data.get("coin", "")
        binance_sym = self.binance_symbol_map.get(coin)
        if not binance_sym:
            return

        raw_bids = data.get("levels", [[], []])[0]
        raw_asks = data.get("levels", [[], []])[1]

        bids = sorted(
            [OrderBookLevel(float(b["px"]), float(b["sz"])) for b in raw_bids],
            key=lambda x: x.price, reverse=True
        )
        asks = sorted(
            [OrderBookLevel(float(a["px"]), float(a["sz"])) for a in raw_asks],
            key=lambda x: x.price
        )

        ob = OrderBook(
            symbol=binance_sym, source="hyperliquid",
            bids=bids[:config.OB_DEPTH_LEVELS],
            asks=asks[:config.OB_DEPTH_LEVELS],
            timestamp=time.time(),
        )
        await self.state.update_order_book(binance_sym, "hyperliquid", ob)


# ─── DATA ENGINE FACTORY ──────────────────────────────────────────────────────

class DataEngine:
    """Top-level data engine that manages both exchanges."""

    def __init__(self, pairs: List[str] = None):
        self.state = MarketState()
        pairs = pairs or config.ACTIVE_PAIRS
        self.binance = BinanceDataEngine(self.state, pairs)
        self.hyperliquid = HyperliquidDataEngine(self.state, pairs)
        self._tasks: List[asyncio.Task] = []

    async def start(self):
        logger.info("[DATA ENGINE] Starting all market data streams...")
        self._tasks = [
            asyncio.create_task(self.binance.start(), name="binance_engine"),
            asyncio.create_task(self.hyperliquid.start(), name="hl_engine"),
        ]
        logger.success("[DATA ENGINE] All streams launched ✓")

    async def stop(self):
        logger.info("[DATA ENGINE] Stopping all streams...")
        await self.binance.stop()
        await self.hyperliquid.stop()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        logger.info("[DATA ENGINE] Stopped.")

    def register_callback(self, fn: Callable):
        self.state.register_callback(fn)
