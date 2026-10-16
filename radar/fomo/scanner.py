"""Token Scanner: ejecuta las vías configuradas (A/B/C), fusiona duplicados y publica en el bus."""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

import httpx

from .. import bus as topics
from ..bus import EventBus
from ..config import Settings
from ..models import TokenInfo
from ..storage import Storage
from ..utils import utcnow
from .base import TokenSource
from .via_a_network import FomoNetworkSource
from .via_b_wrapper import FomoWrapperSource
from .via_c_onchain import build_onchain_sources

log = logging.getLogger(__name__)


def build_token_sources(s: Settings) -> list[TokenSource]:
    sources: list[TokenSource] = []
    if "a" in s.fomo_modes:
        sources.append(FomoNetworkSource(s))
    if "b" in s.fomo_modes:
        sources.append(FomoWrapperSource(s))
    if "c" in s.fomo_modes:
        sources.extend(build_onchain_sources(s))
    return sources


class TokenScanner:
    def __init__(self, settings: Settings, bus: EventBus, storage: Storage, sources: list[TokenSource]) -> None:
        self.s, self.bus, self.storage, self.sources = settings, bus, storage, sources
        self.tokens: dict[str, TokenInfo] = {}
        self.status: dict[str, dict] = {}

    async def run(self) -> None:
        if not self.sources:
            log.warning("No hay fuentes de tokens configuradas (FOMO_MODES)")
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            jobs = []
            for src in self.sources:
                jobs.append(self._poll_loop(src, client))
                stream = src.stream()
                if stream is not None:
                    jobs.append(self._stream_loop(src, stream))
            jobs.append(self._prune_loop())
            await asyncio.gather(*jobs)

    async def _poll_loop(self, src: TokenSource, client: httpx.AsyncClient) -> None:
        delay = self.s.fomo_poll_seconds
        while True:
            try:
                found = await src.poll(client)
                self._set_status(src.name, True, len(found))
                for tok in found:
                    await self.ingest(tok)
                delay = self.s.fomo_poll_seconds
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._set_status(src.name, False, 0, str(exc)[:200])
                log.warning("Fuente de tokens %s falló: %s", src.name, exc)
                delay = min(delay * 2, 120)
            await asyncio.sleep(delay)

    async def _stream_loop(self, src: TokenSource, stream) -> None:
        async for tok in stream:
            self._set_status(f"{src.name}:stream", True, 1)
            await self.ingest(tok)

    async def ingest(self, tok: TokenInfo) -> None:
        if tok.created_at and utcnow() - tok.created_at > timedelta(minutes=self.s.token_max_age_minutes):
            return  # token viejo: no es un lanzamiento reactivo a la noticia
        existing = self.tokens.get(tok.key)
        if existing is None:
            self.tokens[tok.key] = tok
            await self.storage.save_token(tok)
            log.info("🪙 %s ($%s) %s via %s", tok.name, tok.symbol, tok.chain, tok.source)
            self.bus.publish(topics.TOKEN, tok)
        elif existing.merge_metrics(tok):
            self.bus.publish(topics.TOKEN_UPDATE, existing)

    async def _prune_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            cutoff = utcnow() - timedelta(minutes=self.s.token_max_age_minutes)
            for k in [k for k, t in self.tokens.items() if t.first_seen < cutoff]:
                del self.tokens[k]

    def _set_status(self, name: str, ok: bool, n: int, error: str = "") -> None:
        self.status[name] = {"ok": ok, "items": n, "error": error, "at": utcnow().isoformat()}
        self.bus.publish(topics.STATUS, {"component": f"tokens:{name}", **self.status[name]})
