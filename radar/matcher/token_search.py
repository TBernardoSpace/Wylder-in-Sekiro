"""Búsqueda inversa: por cada noticia nueva, busca tokens que YA existían con sus tickers/keywords.

Lección del caso Dacity Coin (16-10-2026): el token se creó en pump.fun 5 días ANTES del tráiler de
GTA 6 que lo mostró. Ningún stream de "tokens nuevos" lo habría visto como nuevo, y el filtro de
antigüedad lo descartaba. Esta búsqueda lo encuentra en DexScreener en cuanto llega la noticia.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Protocol

import httpx

from .. import bus as topics
from ..bus import EventBus
from ..config import Settings
from ..models import NewsItem, TokenInfo
from ..utils import GENERIC_WORDS, utcnow
from .fuzzy import fuzzy_score

log = logging.getLogger(__name__)


class TokenSearcher(Protocol):
    async def search(self, query: str) -> list[TokenInfo]: ...


class Ingestor(Protocol):
    async def ingest(self, tok: TokenInfo, allow_old: bool = False) -> None: ...


def pick_queries(news: NewsItem, limit: int) -> list[str]:
    """Primero los tickers del LLM/heurística (los más específicos), luego nombres propios largos."""
    out: list[str] = []
    for t in news.potential_tickers:
        if 4 <= len(t) <= 12 and t.lower() not in GENERIC_WORDS and not t.isdigit():
            out.append(t)
    for k in sorted(news.keywords, key=len, reverse=True):
        if len(k) >= 5 and k[0].isupper() and k.lower() not in GENERIC_WORDS:
            out.append(k)
    seen: set[str] = set()
    uniq = [q for q in out if not (q.lower() in seen or seen.add(q.lower()))]
    return uniq[:limit]


class NewsTokenSearch:
    def __init__(self, settings: Settings, bus: EventBus, scanner: Ingestor, searcher: TokenSearcher) -> None:
        self.s, self.bus, self.scanner, self.searcher = settings, bus, scanner, searcher
        self._sem = asyncio.Semaphore(2)
        self._tasks: set[asyncio.Task] = set()

    async def run(self) -> None:
        q = self.bus.subscribe()
        while True:
            topic, payload = await q.get()
            if topic == topics.NEWS:  # también narrativas: el token original suele ser anterior a la ola
                task = asyncio.create_task(self.handle(payload))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)

    async def handle(self, news: NewsItem) -> int:
        found = 0
        max_age = timedelta(days=self.s.news_token_search_max_age_days)
        for query in pick_queries(news, self.s.news_token_search_max_queries):
            async with self._sem:
                try:
                    results = await self.searcher.search(query)
                except (httpx.HTTPError, ValueError) as exc:
                    log.info("Búsqueda inversa '%s' falló: %s", query, exc)
                    continue
            for tok in results:
                if tok.chain not in self.s.chains:
                    continue
                if tok.created_at and utcnow() - tok.created_at > max_age:
                    continue  # un token de hace meses no es la jugada de esta noticia
                if fuzzy_score(tok, news)[0] < self.s.fuzzy_prefilter:
                    continue
                tok.source = f"search:{tok.source}"
                await self.scanner.ingest(tok, allow_old=True)
                found += 1
        if found:
            log.info("🔎 búsqueda inversa: %d tokens existentes para «%s»", found, news.headline[:70])
        return found
