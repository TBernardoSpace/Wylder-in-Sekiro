"""Persistencia en SQLite: dedupe de noticias, registro de tokens y anti-spam de alertas.

Se usa sqlite3 de la stdlib en un hilo (asyncio.to_thread) con WAL: suficiente para
cientos de escrituras/segundo y sin dependencias extra. Para multi-proceso, cambiar por Redis.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from typing import Any

from .models import NewsItem, Signal, TokenInfo
from .utils import utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS news (
    id TEXT PRIMARY KEY,
    headline TEXT NOT NULL,
    source TEXT,
    published_at TEXT,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tokens (
    key TEXT PRIMARY KEY,
    symbol TEXT,
    chain TEXT,
    first_seen TEXT,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS alerts (
    token_key TEXT PRIMARY KEY,
    news_id TEXT NOT NULL,
    signal_id TEXT NOT NULL,
    match_score REAL,
    alerted_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS signals (
    id TEXT PRIMARY KEY,
    status TEXT,
    created_at TEXT,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_signals_created ON signals(created_at);
"""


class Storage:
    def __init__(self, path: str = "radar.db") -> None:
        self.path = path
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()

    async def init(self) -> None:
        def _init() -> None:
            self._conn = sqlite3.connect(self.path, check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(SCHEMA)
            self._conn.commit()

        await asyncio.to_thread(_init)

    async def close(self) -> None:
        if self._conn:
            await asyncio.to_thread(self._conn.close)
            self._conn = None

    async def _exec(self, sql: str, params: tuple = (), fetch: bool = False) -> list[tuple[Any, ...]]:
        def _run() -> list[tuple[Any, ...]]:
            assert self._conn is not None, "Storage.init() no fue llamado"
            with self._lock:
                cur = self._conn.execute(sql, params)
                rows = cur.fetchall() if fetch else []
                self._conn.commit()
                return rows

        return await asyncio.to_thread(_run)

    # --- Noticias ---
    async def news_seen(self, news_id: str) -> bool:
        return bool(await self._exec("SELECT 1 FROM news WHERE id=?", (news_id,), fetch=True))

    async def save_news(self, item: NewsItem) -> None:
        await self._exec(
            "INSERT OR REPLACE INTO news(id, headline, source, published_at, payload) VALUES (?,?,?,?,?)",
            (item.id, item.headline, item.source, item.published_at.isoformat(), item.model_dump_json()),
        )

    # --- Tokens ---
    async def save_token(self, token: TokenInfo) -> None:
        await self._exec(
            "INSERT OR REPLACE INTO tokens(key, symbol, chain, first_seen, payload) VALUES (?,?,?,?,?)",
            (token.key, token.symbol, token.chain, token.first_seen.isoformat(), token.model_dump_json()),
        )

    # --- Alertas (anti-spam) ---
    async def was_alerted(self, token_key: str) -> bool:
        return bool(await self._exec("SELECT 1 FROM alerts WHERE token_key=?", (token_key,), fetch=True))

    async def mark_alerted(self, signal: Signal) -> bool:
        """Registra la alerta de forma atómica. Devuelve False si ese token ya había sido alertado."""
        def _run() -> bool:
            assert self._conn is not None
            with self._lock:
                cur = self._conn.execute(
                    "INSERT OR IGNORE INTO alerts(token_key, news_id, signal_id, match_score, alerted_at)"
                    " VALUES (?,?,?,?,?)",
                    (signal.token.key, signal.news.id, signal.id, signal.match_score, utcnow().isoformat()),
                )
                self._conn.commit()
                return cur.rowcount == 1

        return await asyncio.to_thread(_run)

    # --- Señales ---
    async def save_signal(self, signal: Signal) -> None:
        await self._exec(
            "INSERT OR REPLACE INTO signals(id, status, created_at, payload) VALUES (?,?,?,?)",
            (signal.id, signal.status, signal.created_at.isoformat(), json.dumps(signal.dump())),
        )

    async def recent_signals(self, limit: int = 50) -> list[dict]:
        rows = await self._exec(
            "SELECT payload FROM signals ORDER BY created_at DESC LIMIT ?", (limit,), fetch=True
        )
        return [json.loads(r[0]) for r in rows]
