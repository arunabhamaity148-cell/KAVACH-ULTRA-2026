"""
KAVACH-ULTRA 2026 — utils/database.py
Persistent trade logging and performance analytics via aiosqlite.
"""

import asyncio
import time
from typing import Optional, Dict, List, Any

import aiosqlite
from loguru import logger

import config


class TradeDatabase:

    def __init__(self, db_path: str = config.DB_PATH):
        self.db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None
        self._lock = asyncio.Lock()

    async def start(self):
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._create_tables()
        logger.info(f"[DB] Database ready: {self.db_path}")

    async def stop(self):
        if self._db:
            await self._db.close()

    async def _create_tables(self):
        async with self._lock:
            await self._db.executescript("""
                CREATE TABLE IF NOT EXISTS trades (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol          TEXT    NOT NULL,
                    direction       TEXT    NOT NULL,
                    strategy        TEXT    NOT NULL,
                    entry_price     REAL    NOT NULL,
                    exit_price      REAL,
                    stop_loss       REAL    NOT NULL,
                    take_profit     REAL    NOT NULL,
                    quantity        REAL    NOT NULL,
                    usdt_risk       REAL    NOT NULL,
                    leverage        INTEGER NOT NULL,
                    risk_pct        REAL    NOT NULL,
                    signal_confidence REAL  NOT NULL,
                    pnl             REAL,
                    exit_reason     TEXT,
                    status          TEXT    NOT NULL DEFAULT 'OPEN',
                    opened_at       REAL    NOT NULL,
                    closed_at       REAL
                );

                CREATE TABLE IF NOT EXISTS signals (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol          TEXT    NOT NULL,
                    strategy        TEXT    NOT NULL,
                    direction       TEXT    NOT NULL,
                    confidence      REAL    NOT NULL,
                    ai_approved     INTEGER NOT NULL,
                    ai_reason       TEXT,
                    sentiment_score REAL,
                    entry_price     REAL,
                    executed        INTEGER NOT NULL DEFAULT 0,
                    reject_reason   TEXT,
                    created_at      REAL    NOT NULL
                );

                CREATE TABLE IF NOT EXISTS daily_stats (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    date            TEXT    NOT NULL UNIQUE,
                    total_trades    INTEGER NOT NULL DEFAULT 0,
                    winning_trades  INTEGER NOT NULL DEFAULT 0,
                    losing_trades   INTEGER NOT NULL DEFAULT 0,
                    gross_pnl       REAL    NOT NULL DEFAULT 0,
                    net_pnl         REAL    NOT NULL DEFAULT 0,
                    max_drawdown    REAL    NOT NULL DEFAULT 0,
                    win_rate        REAL    NOT NULL DEFAULT 0,
                    avg_rr          REAL    NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS sentiment_log (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    score           REAL    NOT NULL,
                    label           TEXT    NOT NULL,
                    black_swan      INTEGER NOT NULL DEFAULT 0,
                    black_swan_reason TEXT,
                    confidence      REAL    NOT NULL,
                    key_headlines   TEXT,
                    created_at      REAL    NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
                CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
                CREATE INDEX IF NOT EXISTS idx_signals_created ON signals(created_at);
            """)
            await self._db.commit()

    # ─── TRADE OPERATIONS ────────────────────────────────────────────────────

    async def log_trade_open(self, order: Any) -> int:
        async with self._lock:
            cursor = await self._db.execute(
                """INSERT INTO trades
                   (symbol, direction, strategy, entry_price, stop_loss, take_profit,
                    quantity, usdt_risk, leverage, risk_pct, signal_confidence, status, opened_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    order.symbol, order.direction, order.strategy,
                    order.entry_price, order.stop_loss, order.take_profit,
                    order.quantity, order.usdt_risk, order.leverage,
                    order.risk_pct, order.signal_confidence, "OPEN", order.timestamp,
                )
            )
            await self._db.commit()
            return cursor.lastrowid

    async def log_trade_close(
        self,
        symbol: str,
        exit_price: float,
        pnl: float,
        exit_reason: str,
    ):
        async with self._lock:
            await self._db.execute(
                """UPDATE trades
                   SET exit_price=?, pnl=?, exit_reason=?, status='CLOSED', closed_at=?
                   WHERE symbol=? AND status='OPEN'""",
                (exit_price, pnl, exit_reason, time.time(), symbol)
            )
            await self._db.commit()
        await self._update_daily_stats()

    async def log_signal(
        self,
        symbol: str,
        strategy: str,
        direction: str,
        confidence: float,
        ai_approved: bool,
        ai_reason: str,
        sentiment_score: float,
        entry_price: float,
        executed: bool,
        reject_reason: str = "",
    ):
        async with self._lock:
            await self._db.execute(
                """INSERT INTO signals
                   (symbol, strategy, direction, confidence, ai_approved, ai_reason,
                    sentiment_score, entry_price, executed, reject_reason, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    symbol, strategy, direction, confidence,
                    1 if ai_approved else 0, ai_reason,
                    sentiment_score, entry_price,
                    1 if executed else 0, reject_reason, time.time(),
                )
            )
            await self._db.commit()

    async def log_sentiment(self, sentiment: Any):
        import json
        async with self._lock:
            await self._db.execute(
                """INSERT INTO sentiment_log
                   (score, label, black_swan, black_swan_reason, confidence, key_headlines, created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    sentiment.score, sentiment.label,
                    1 if sentiment.black_swan else 0,
                    sentiment.black_swan_reason, sentiment.confidence,
                    json.dumps(sentiment.key_headlines), sentiment.timestamp,
                )
            )
            await self._db.commit()

    # ─── ANALYTICS ───────────────────────────────────────────────────────────

    async def _update_daily_stats(self):
        from datetime import date
        today = date.today().isoformat()
        async with self._lock:
            cursor = await self._db.execute(
                """SELECT COUNT(*) total,
                          SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) wins,
                          SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) losses,
                          SUM(pnl) gross_pnl,
                          AVG(CASE WHEN pnl IS NOT NULL THEN
                              ABS((take_profit - entry_price) / (entry_price - stop_loss))
                          END) avg_rr
                   FROM trades
                   WHERE date(opened_at, 'unixepoch') = ? AND status='CLOSED'""",
                (today,)
            )
            row = await cursor.fetchone()
            if not row or not row["total"]:
                return

            total    = row["total"]
            wins     = row["wins"] or 0
            losses   = row["losses"] or 0
            gross    = row["gross_pnl"] or 0
            avg_rr   = row["avg_rr"] or 0
            win_rate = wins / total if total > 0 else 0

            await self._db.execute(
                """INSERT INTO daily_stats
                   (date, total_trades, winning_trades, losing_trades,
                    gross_pnl, net_pnl, win_rate, avg_rr)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(date) DO UPDATE SET
                     total_trades=excluded.total_trades,
                     winning_trades=excluded.winning_trades,
                     losing_trades=excluded.losing_trades,
                     gross_pnl=excluded.gross_pnl,
                     net_pnl=excluded.net_pnl,
                     win_rate=excluded.win_rate,
                     avg_rr=excluded.avg_rr""",
                (today, total, wins, losses, gross, gross, win_rate, avg_rr)
            )
            await self._db.commit()

    async def get_performance_summary(self) -> Dict:
        """Returns overall bot performance stats."""
        async with self._lock:
            cursor = await self._db.execute(
                """SELECT COUNT(*) total,
                          SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) wins,
                          SUM(pnl) total_pnl,
                          MAX(pnl) best_trade,
                          MIN(pnl) worst_trade
                   FROM trades WHERE status='CLOSED'"""
            )
            row = await cursor.fetchone()
            if not row:
                return {}

            total = row["total"] or 0
            wins  = row["wins"] or 0

            return {
                "total_trades": total,
                "win_rate": round(wins / total * 100, 1) if total else 0,
                "total_pnl": round(row["total_pnl"] or 0, 2),
                "best_trade": round(row["best_trade"] or 0, 2),
                "worst_trade": round(row["worst_trade"] or 0, 2),
            }

    async def get_recent_trades(self, limit: int = 10) -> List[Dict]:
        async with self._lock:
            cursor = await self._db.execute(
                """SELECT symbol, direction, strategy, entry_price, exit_price,
                          pnl, status, exit_reason, opened_at, closed_at
                   FROM trades ORDER BY opened_at DESC LIMIT ?""",
                (limit,)
            )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]
