"""Conectores concretos de noticias: CryptoPanic, NewsAPI, GDELT, RSS y WebSocket genérico.

Todos los parsers son tolerantes: si el proveedor cambia un campo, se prueba con alternativas.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncIterator

import feedparser
import httpx

from ..utils import first_path, to_datetime, utcnow
from .base import PollingNewsSource, RawNews, StreamingNewsSource

log = logging.getLogger(__name__)


class CryptoPanicSource(PollingNewsSource):
    """https://cryptopanic.com/developers/api/ — plan gratuito con token."""

    name = "cryptopanic"

    def __init__(self, token: str, url: str) -> None:
        self.token, self.url = token, url

    async def fetch(self, client: httpx.AsyncClient) -> list[RawNews]:
        r = await client.get(self.url, params={"auth_token": self.token, "public": "true", "kind": "news"})
        r.raise_for_status()
        out = []
        for it in r.json().get("results", []):
            title = it.get("title")
            if not title:
                continue
            src = first_path(it, ["source.title", "source.domain", "domain"]) or "CryptoPanic"
            out.append(RawNews(
                headline=title,
                summary=it.get("description") or "",
                source=f"CryptoPanic/{src}",
                url=first_path(it, ["original_url", "url"]),
                published_at=to_datetime(first_path(it, ["published_at", "created_at"])) or utcnow(),
            ))
        return out


class NewsAPISource(PollingNewsSource):
    """https://newsapi.org — top-headlines (o everything si hay query)."""

    name = "newsapi"

    def __init__(self, key: str, query: str = "", country: str = "us") -> None:
        self.key, self.query, self.country = key, query, country

    async def fetch(self, client: httpx.AsyncClient) -> list[RawNews]:
        if self.query:
            url = "https://newsapi.org/v2/everything"
            params = {"q": self.query, "sortBy": "publishedAt", "pageSize": 50, "language": "en"}
        else:
            url = "https://newsapi.org/v2/top-headlines"
            params = {"country": self.country, "pageSize": 50}
        r = await client.get(url, params=params, headers={"X-Api-Key": self.key})
        r.raise_for_status()
        out = []
        for a in r.json().get("articles", []):
            if not a.get("title") or a["title"] == "[Removed]":
                continue
            out.append(RawNews(
                headline=a["title"],
                summary=a.get("description") or "",
                source=f"NewsAPI/{first_path(a, ['source.name']) or '?'}",
                url=a.get("url"),
                published_at=to_datetime(a.get("publishedAt")) or utcnow(),
            ))
        return out


class GDELTSource(PollingNewsSource):
    """GDELT DOC 2.0 API — gratuito, sin clave, ~15 min de latencia. Útil como red de seguridad."""

    name = "gdelt"

    def __init__(self, query: str) -> None:
        self.query = query

    async def fetch(self, client: httpx.AsyncClient) -> list[RawNews]:
        r = await client.get(
            "https://api.gdeltproject.org/api/v2/doc/doc",
            params={"query": self.query, "mode": "artlist", "format": "json", "maxrecords": 50,
                    "sort": "datedesc", "timespan": "30min"},
        )
        r.raise_for_status()
        try:
            data = r.json()
        except json.JSONDecodeError:  # GDELT responde texto plano ante queries inválidas
            log.warning("GDELT: respuesta no JSON: %.120s", r.text)
            return []
        return [
            RawNews(
                headline=a["title"],
                source=f"GDELT/{a.get('domain', '?')}",
                url=a.get("url"),
                published_at=to_datetime(a.get("seendate")) or utcnow(),
            )
            for a in data.get("articles", []) if a.get("title")
        ]


class RSSSource(PollingNewsSource):
    """Cualquier feed RSS/Atom. Para cuentas de X (WatcherGuru, Tree of Alpha, DB...) usa un puente
    RSS (RSSHub self-hosted: /twitter/user/WatcherGuru, o nitter/rss.app)."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.name = f"rss:{httpx.URL(url).host}"

    async def fetch(self, client: httpx.AsyncClient) -> list[RawNews]:
        r = await client.get(self.url)
        r.raise_for_status()
        feed = await asyncio.to_thread(feedparser.parse, r.content)
        title = feed.feed.get("title") or self.name
        out = []
        for e in feed.entries[:50]:
            headline = (e.get("title") or e.get("summary") or "").strip()
            if not headline:
                continue
            published = to_datetime(e.get("published") or e.get("updated")) or utcnow()
            out.append(RawNews(headline=headline[:400], summary=(e.get("summary") or "")[:600],
                               source=f"RSS/{title}", url=e.get("link"), published_at=published))
        return out


class WebSocketNewsSource(StreamingNewsSource):
    """Feed push genérico (ej. Tree of Alpha `wss://news.treeofalpha.com/ws`, o tu propio relay de X).

    Acepta mensajes JSON con campos tipo title/body/source/url/time. Reconecta con backoff."""

    name = "websocket"

    def __init__(self, url: str, headers: dict | None = None, subscribe: list | None = None) -> None:
        self.url, self.headers, self.subscribe = url, headers or {}, subscribe or []

    async def stream(self) -> AsyncIterator[RawNews]:
        import websockets

        backoff = 1.0
        while True:
            try:
                async with websockets.connect(self.url, additional_headers=self.headers,
                                              ping_interval=20, max_size=2**22) as ws:
                    log.info("WS noticias conectado: %s", self.url)
                    backoff = 1.0
                    for msg in self.subscribe:
                        await ws.send(json.dumps(msg) if not isinstance(msg, str) else msg)
                    async for raw in ws:
                        item = self._parse(raw)
                        if item:
                            yield item
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - cualquier fallo de red => reconectar
                log.warning("WS noticias caído (%s); reintento en %.0fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    @staticmethod
    def _parse(raw: str | bytes) -> RawNews | None:
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(data, dict):
            return None
        headline = first_path(data, ["title", "headline", "en", "body", "text", "data.title"])
        if not headline:
            return None
        return RawNews(
            headline=str(headline)[:400],
            summary=str(first_path(data, ["body", "description", "summary"]) or "")[:600],
            source=f"WS/{first_path(data, ['source', 'info.twitterId', 'author']) or 'feed'}",
            url=first_path(data, ["url", "link"]),
            published_at=to_datetime(first_path(data, ["time", "timestamp", "published_at", "date"])) or utcnow(),
        )
