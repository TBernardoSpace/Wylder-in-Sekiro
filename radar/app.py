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
from .matcher.narratives import NarrativeDetector
from .matcher.token_search import NewsTokenSearch
from .onchain.dexscreener import DexScreenerClient
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
        # En replay el LLM es opcional (LLM_ENABLED): permite medir también el juez real
        if settings.llm_enabled and settings.anthropic_configured and not settings.demo_mode:
            self.llm = LLMClient(settings)
        elif settings.llm_enabled and not settings.offline:
            log.warning("ANTHROPIC_API_KEY no definido: se usará solo heurística + fuzzy matching")
        self.recent_news: deque[NewsItem] = deque(maxlen=150)
        self.recent_tokens: dict[str, TokenInfo] = {}
        self.recent_signals: dict[str, Signal] = {}
        self.status: dict[str, dict] = {}
        self._tasks: list[asyncio.Task] = []
        self._http = httpx.AsyncClient(timeout=8)
        self.alerts = AlertDispatcher(settings)
        if settings.replay_path:
            self.alerts.targets = []  # un replay nunca manda alertas reales a Telegram/Discord
        self.scanner: TokenScanner | None = None
        self._bridge_parser = JsonHttpTokenSource(
            name="fomo:bridge", url="", items_path=settings.fomo_a_items_path,
            field_map=settings.fomo_a_field_map, default_chain=settings.fomo_a_default_chain,
            url_template=settings.fomo_token_url_template, on_fomo=True)

    def _build(self) -> tuple[NewsWatcher, TokenScanner, MatchEngine]:
        s = self.s
        searcher: Any = None
        if s.replay_path:
            from .replay import (ReplayClock, ReplayNewsSource, ReplaySearch, ReplaySecurity,
                                 ReplayTokenSource, Scenario)

            sc = Scenario.load(s.replay_path)
            self.replay_clock = ReplayClock(s.replay_speed, sc.start)
            polling, streaming = [], [ReplayNewsSource(sc, self.replay_clock)]
            token_sources = [ReplayTokenSource(sc, self.replay_clock, s.fomo_token_url_template)]
            security = ReplaySecurity(sc)
            searcher = ReplaySearch(sc, self.replay_clock, s.fomo_token_url_template)
        elif s.demo_mode:
            from .demo import DemoNewsSource, DemoSecurity, DemoTokenSource

            polling, streaming = [], [DemoNewsSource()]
            s.narrative_warmup_min = 0  # en la demo no hay línea base que esperar
            token_sources = [DemoTokenSource(s)]
            security: Any = DemoSecurity()
        else:
            polling, streaming = build_news_sources(s)
            token_sources = build_token_sources(s)
            security = SecurityChecker(self._http)
            searcher = DexScreenerClient(self._http, s.fomo_token_url_template)
        watcher = NewsWatcher(s, self.bus, self.storage, self.llm, polling, streaming)
        scanner = TokenScanner(s, self.bus, self.storage, token_sources)
        history = MetricsHistory()
        engine = MatchEngine(s, self.bus, self.storage, self.llm, SafetyFilter(s, history, security),
                             history, self.alerts)
        self.narratives = NarrativeDetector(s, self.bus, self.storage) if s.narrative_enabled else None
        self.token_search = (NewsTokenSearch(s, self.bus, scanner, searcher)
                             if s.news_token_search and searcher is not None else None)
        return watcher, scanner, engine

    async def start(self) -> None:
        await self.storage.init()
        watcher, scanner, engine = self._build()
        self.scanner = scanner
        state_q = self.bus.subscribe()
        jobs = [("state", self._track_state(state_q)), ("engine", engine.run()),
                ("scanner", scanner.run()), ("watcher", watcher.run())]
        if self.narratives:
            jobs.append(("narratives", self.narratives.run()))
        if self.token_search:
            jobs.append(("token_search", self.token_search.run()))
        for name, coro in jobs:
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
