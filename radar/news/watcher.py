"""News Watcher: orquesta las fuentes, deduplica, extrae entidades y publica en el bus."""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

import httpx

from .. import bus as topics
from ..bus import EventBus
from ..config import Settings
from ..llm import LLMClient
from ..storage import Storage
from ..utils import headline_hash, utcnow
from .base import PollingNewsSource, RawNews, StreamingNewsSource
from .extractor import EntityExtractor
from .sources import CryptoPanicSource, GDELTSource, NewsAPISource, RSSSource, WebSocketNewsSource

log = logging.getLogger(__name__)


def build_news_sources(s: Settings) -> tuple[list[PollingNewsSource], list[StreamingNewsSource]]:
    polling: list[PollingNewsSource] = []
    streaming: list[StreamingNewsSource] = []
    if s.cryptopanic_token:
        polling.append(CryptoPanicSource(s.cryptopanic_token, s.cryptopanic_url))
    if s.newsapi_key:
        polling.append(NewsAPISource(s.newsapi_key, s.newsapi_query, s.newsapi_country))
    if s.gdelt_enabled:
        polling.append(GDELTSource(s.gdelt_query))
    polling.extend(RSSSource(u) for u in s.rss_feeds)
    if s.ws_news_url:
        streaming.append(WebSocketNewsSource(s.ws_news_url, s.ws_news_headers, s.ws_news_subscribe))
    return polling, streaming


class NewsWatcher:
    def __init__(self, settings: Settings, bus: EventBus, storage: Storage, llm: LLMClient | None,
                 polling: list[PollingNewsSource], streaming: list[StreamingNewsSource]) -> None:
        self.s, self.bus, self.storage = settings, bus, storage
        self.extractor = EntityExtractor(llm)
        self.polling, self.streaming = polling, streaming
        self._seen: set[str] = set()
        self._tasks: set[asyncio.Task] = set()
        self.status: dict[str, dict] = {}

    async def run(self) -> None:
        if not self.polling and not self.streaming:
            log.warning("No hay fuentes de noticias configuradas (ver .env.example)")
        async with httpx.AsyncClient(timeout=10, follow_redirects=True,
                                     headers={"User-Agent": "news-token-radar/1.0"}) as client:
            jobs = [self._poll_loop(src, client) for src in self.polling]
            jobs += [self._stream_loop(src) for src in self.streaming]
            await asyncio.gather(*jobs)

    async def _poll_loop(self, src: PollingNewsSource, client: httpx.AsyncClient) -> None:
        backoff = self.s.news_poll_seconds
        while True:
            try:
                items = await src.fetch(client)
                self._set_status(src.name, ok=True, n=len(items))
                # más antiguas primero, para que el dashboard quede ordenado
                for raw in sorted(items, key=lambda x: x.published_at):
                    self.ingest(raw)
                backoff = self.s.news_poll_seconds
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._set_status(src.name, ok=False, error=str(exc)[:200])
                log.warning("Fuente %s falló: %s", src.name, exc)
                backoff = min(backoff * 2, 120)
            await asyncio.sleep(backoff)

    async def _stream_loop(self, src: StreamingNewsSource) -> None:
        self._set_status(src.name, ok=True, n=0)
        async for raw in src.stream():
            self.ingest(raw)

    def ingest(self, raw: RawNews) -> None:
        """Dedupe síncrono (barato) y procesamiento LLM en una tarea aparte (no bloquea el polling)."""
        nid = headline_hash(raw.headline)
        if nid in self._seen:
            return
        self._seen.add(nid)
        if utcnow() - raw.published_at > timedelta(minutes=self.s.news_max_age_minutes):
            return
        task = asyncio.create_task(self._process(nid, raw))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _process(self, nid: str, raw: RawNews) -> None:
        try:
            if await self.storage.news_seen(nid):
                return
            item = await self.extractor.process(raw)
            await self.storage.save_news(item)
            log.info("📰 %s | tickers=%s", item.headline[:90], item.potential_tickers[:6])
            self.bus.publish(topics.NEWS, item)
        except Exception:  # noqa: BLE001
            log.exception("Error procesando noticia: %s", raw.headline[:80])

    def _set_status(self, name: str, ok: bool, n: int = 0, error: str = "") -> None:
        self.status[name] = {"ok": ok, "items": n, "error": error, "at": utcnow().isoformat()}
        self.bus.publish(topics.STATUS, {"component": f"news:{name}", **self.status[name]})
