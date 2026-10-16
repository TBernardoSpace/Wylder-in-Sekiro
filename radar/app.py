"""Ensamblado del sistema: crea los componentes, los conecta al bus y mantiene el estado reciente
que consume el dashboard."""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import Any

import httpx

from . import bus as topics
from .alerts.dispatcher import AlertDispatcher
from .bus import EventBus
from .config import Settings
from .fomo.base import JsonHttpTokenSource
from .fomo.scanner import TokenScanner, build_token_sources
from .llm import LLMClient
from .matcher.engine import MatchEngine
from .matcher.safety import MetricsHistory, SafetyFilter
from .models import NewsItem, Signal, TokenInfo
from .news.watcher import NewsWatcher, build_news_sources
from .onchain.security import SecurityChecker
from .storage import Storage

log = logging.getLogger(__name__)


class Radar:
    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self.bus = EventBus()
        self.storage = Storage(settings.db_path)
        self.llm: LLMClient | None = None
        if settings.llm_enabled and settings.anthropic_configured and not settings.demo_mode:
            self.llm = LLMClient(settings)
        elif settings.llm_enabled and not settings.demo_mode:
            log.warning("ANTHROPIC_API_KEY no definido: se usará solo heurística + fuzzy matching")
        self.recent_news: deque[NewsItem] = deque(maxlen=150)
        self.recent_tokens: dict[str, TokenInfo] = {}
        self.recent_signals: dict[str, Signal] = {}
        self.status: dict[str, dict] = {}
        self._tasks: list[asyncio.Task] = []
        self._http = httpx.AsyncClient(timeout=8)
        self.alerts = AlertDispatcher(settings)
        self.scanner: TokenScanner | None = None
        self._bridge_parser = JsonHttpTokenSource(
            name="fomo:bridge", url="", items_path=settings.fomo_a_items_path,
            field_map=settings.fomo_a_field_map, default_chain=settings.fomo_a_default_chain,
            url_template=settings.fomo_token_url_template, on_fomo=True)

    def _build(self) -> tuple[NewsWatcher, TokenScanner, MatchEngine]:
        s = self.s
        if s.demo_mode:
            from .demo import DemoNewsSource, DemoSecurity, DemoTokenSource

            polling, streaming = [], [DemoNewsSource()]
            token_sources = [DemoTokenSource(s)]
            security: Any = DemoSecurity()
        else:
            polling, streaming = build_news_sources(s)
            token_sources = build_token_sources(s)
            security = SecurityChecker(self._http)
        watcher = NewsWatcher(s, self.bus, self.storage, self.llm, polling, streaming)
        scanner = TokenScanner(s, self.bus, self.storage, token_sources)
        history = MetricsHistory()
        engine = MatchEngine(s, self.bus, self.storage, self.llm, SafetyFilter(s, history, security),
                             history, self.alerts)
        return watcher, scanner, engine

    async def start(self) -> None:
        await self.storage.init()
        watcher, scanner, engine = self._build()
        self.scanner = scanner
        state_q = self.bus.subscribe()
        for name, coro in (("state", self._track_state(state_q)), ("engine", engine.run()),
                           ("scanner", scanner.run()), ("watcher", watcher.run())):
            self._tasks.append(asyncio.create_task(self._supervise(name, coro), name=name))
        log.info("Radar iniciado · demo=%s · LLM=%s · vías fomo=%s", self.s.demo_mode,
                 self.llm.model if self.llm else "off", self.s.fomo_modes)

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.alerts.close()
        await self._http.aclose()
        await self.storage.close()

    async def _supervise(self, name: str, coro) -> None:
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Componente %s se cayó", name)
            self.bus.publish(topics.STATUS, {"component": name, "ok": False, "error": "crashed"})

    async def _track_state(self, q: asyncio.Queue) -> None:
        while True:
            topic, payload = await q.get()
            if topic == topics.NEWS:
                self.recent_news.appendleft(payload)
            elif topic in (topics.TOKEN, topics.TOKEN_UPDATE):
                self.recent_tokens[payload.key] = payload
                if len(self.recent_tokens) > 300:
                    oldest = min(self.recent_tokens.values(), key=lambda t: t.first_seen)
                    del self.recent_tokens[oldest.key]
            elif topic == topics.SIGNAL:
                self.recent_signals[payload.id] = payload
                if len(self.recent_signals) > 200:
                    oldest_sig = min(self.recent_signals.values(), key=lambda x: x.created_at)
                    del self.recent_signals[oldest_sig.id]
            elif topic == topics.STATUS:
                self.status[payload["component"]] = payload

    async def ingest_fomo_payload(self, url: str, payload: Any) -> int:
        """Respuestas JSON de prod-api.fomo.family reenviadas por el userscript (Vía A en navegador)."""
        if self.scanner is None:
            return 0
        tokens = self._bridge_parser.parse_payload(payload)
        for tok in tokens:
            await self.scanner.ingest(tok)
        self.bus.publish(topics.STATUS, {"component": "tokens:fomo:bridge", "ok": True, "items": len(tokens),
                                         "error": "" if tokens else f"0 tokens reconocidos en {url[:80]}"})
        if not tokens:
            sample = payload if isinstance(payload, dict) else (payload[:1] if isinstance(payload, list) else payload)
            log.info("Bridge: no se reconocieron tokens en %s. Muestra: %.300s", url, sample)
        return len(tokens)

    def snapshot(self) -> dict:
        tokens = sorted(self.recent_tokens.values(), key=lambda t: t.first_seen, reverse=True)[:150]
        signals = sorted(self.recent_signals.values(), key=lambda x: x.created_at, reverse=True)
        return {
            "news": [n.model_dump(mode="json") for n in self.recent_news],
            "tokens": [t.model_dump(mode="json") for t in tokens],
            "signals": [x.dump() for x in signals],
            "status": self.status,
            "config": {
                "match_threshold": self.s.match_threshold,
                "min_liquidity_usd": self.s.min_liquidity_usd,
                "llm": self.llm.model if self.llm else None,
                "fomo_modes": self.s.fomo_modes,
                "demo": self.s.demo_mode,
            },
        }
