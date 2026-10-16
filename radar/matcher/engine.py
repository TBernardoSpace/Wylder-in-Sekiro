"""The Matcher: cruza noticias × tokens, consulta al LLM, aplica filtros y decide qué alertar.

Flujo por par (token, noticia):
  fuzzy >= FUZZY_PREFILTER  ->  juicio LLM (batch de noticias por token)  ->  score final
  score >= MATCH_THRESHOLD  ->  enriquecer métricas (DexScreener) -> filtro de seguridad
     pasa  -> ¿es el "líder" de la noticia (más liquidez)?  sí -> ALERTA (una sola vez por token)
                                                             no -> 'secondary' (solo dashboard)
     no pasa -> 'watch' (se re-evalúa cada RECHECK_SECONDS durante RECHECK_MINUTES) o 'rejected'
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict, defaultdict
from datetime import timedelta
from typing import Protocol

import httpx

from .. import bus as topics
from ..bus import EventBus
from ..config import Settings
from ..llm import LLMClient
from ..models import NewsItem, Signal, TokenInfo
from ..onchain.dexscreener import DexScreenerClient
from ..storage import Storage
from ..utils import utcnow
from .fuzzy import fuzzy_score
from .safety import MetricsHistory, SafetyFilter

log = logging.getLogger(__name__)


class AlertSender(Protocol):
    async def send(self, signal: Signal, *, new_leader: bool = False) -> None: ...


class MatchEngine:
    def __init__(self, settings: Settings, bus: EventBus, storage: Storage, llm: LLMClient | None,
                 safety: SafetyFilter, history: MetricsHistory, alerts: AlertSender) -> None:
        self.s, self.bus, self.storage, self.llm = settings, bus, storage, llm
        self.safety, self.history, self.alerts = safety, history, alerts
        self.news: OrderedDict[str, NewsItem] = OrderedDict()
        self.tokens: dict[str, TokenInfo] = {}
        self.signals: dict[tuple[str, str], Signal] = {}       # (token_key, news_id) -> Signal
        self.leaders: dict[str, tuple[str, float]] = {}         # news_id -> (token_key, liquidez)
        self._token_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._promote_lock = asyncio.Lock()
        self._evaluating: set[str] = set()
        self._last_enrich: dict[str, float] = {}
        self._rematched: set[str] = set()
        self._tasks: set[asyncio.Task] = set()
        self._dex: DexScreenerClient | None = None

    # ------------------------------------------------------------------ loop principal
    async def run(self) -> None:
        q = self.bus.subscribe()
        async with httpx.AsyncClient(timeout=8) as client:
            if self.s.enrich_with_dexscreener and not self.s.demo_mode:
                self._dex = DexScreenerClient(client, self.s.fomo_token_url_template)
            await asyncio.gather(self._consume(q), self._recheck_loop())

    async def _consume(self, q: asyncio.Queue) -> None:
        while True:
            topic, payload = await q.get()
            if topic == topics.NEWS:
                self._spawn(self.on_news(payload))
            elif topic == topics.TOKEN:
                self._spawn(self.on_token(payload))
            elif topic == topics.TOKEN_UPDATE:
                self.tokens[payload.key] = payload
                self.history.record(payload)
                if payload.description or payload.twitter or payload.website:
                    if payload.key not in self._rematched:  # llegó la metadata: cruzar de nuevo
                        self._rematched.add(payload.key)
                        self._spawn(self.match_token(payload, list(self.news.values())))
                for sig in self._signals_for(payload.key, statuses=("watch",)):
                    self._spawn(self.evaluate(sig))

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._on_task_done)

    def _on_task_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception():
            log.error("Error en tarea del motor", exc_info=task.exception())

    # ------------------------------------------------------------------ eventos
    async def on_news(self, news: NewsItem) -> None:
        self.news[news.id] = news
        self._prune()
        # Cruce "hacia atrás": tokens creados ANTES de que nos llegara la noticia (es habitual).
        # Los candidatos se evalúan de mayor a menor liquidez, para que el primero en alertar sea
        # el que de verdad concentra el dinero y no el que el scheduler procesó antes.
        found: list[Signal] = []
        await asyncio.gather(*(self.match_token(t, [news], collect=found) for t in list(self.tokens.values())))
        await self._enrich([s.token for s in found])
        for sig in sorted(found, key=lambda x: -(x.token.liquidity_usd or 0)):
            await self.evaluate(sig)

    async def on_token(self, token: TokenInfo) -> None:
        self.tokens[token.key] = token
        self.history.record(token)
        await self.match_token(token, list(self.news.values()))

    # ------------------------------------------------------------------ matching
    async def match_token(self, token: TokenInfo, news_list: list[NewsItem],
                          collect: list[Signal] | None = None) -> None:
        async with self._token_locks[token.key]:
            pairs = []
            for n in news_list:
                if (token.key, n.id) in self.signals:
                    continue
                f, why = fuzzy_score(token, n)
                if f >= self.s.fuzzy_prefilter:
                    pairs.append((n, f, why))
            if not pairs:
                return
            pairs.sort(key=lambda x: x[1], reverse=True)
            pairs = pairs[: self.s.max_news_per_judge]

            # El juez LLM solo evalúa noticias reales: las narrativas emergentes salen de los propios
            # tokens y un titular sintético no le aporta contexto.
            verdicts: dict[str, tuple[int, str]] = {}
            judge = [n for n, _, _ in pairs if n.extractor != "cluster"]
            if self.llm and judge:
                links = [x for x in (token.twitter, token.website, token.telegram) if x]
                res = await self.llm.judge(token.name, token.symbol, [n.headline for n in judge],
                                           token.description or "", links)
                if res:
                    verdicts = {n.id: v for n, v in zip(judge, res, strict=True)}

            for n, f, why in pairs:
                llm_score, reason = verdicts.get(n.id, (None, why))
                if llm_score is None:
                    final = f
                else:
                    final = self.s.llm_weight * llm_score + (1 - self.s.llm_weight) * f
                final = round(final, 1)
                log.debug("par %s × %s fuzzy=%.0f llm=%s final=%.1f", token.symbol, n.headline[:40], f,
                          llm_score, final)
                if final < self.s.match_threshold:
                    continue
                sig = Signal(news=n, token=token, fuzzy_score=f, llm_score=llm_score, match_score=final,
                             reason=reason or why)
                self.signals[(token.key, n.id)] = sig
                log.info("🎯 MATCH %.0f  $%s  <->  %s", final, token.symbol, n.headline[:80])
                if collect is not None:
                    collect.append(sig)
                else:
                    self._spawn(self.evaluate(sig))

    # ------------------------------------------------------------------ seguridad + decisión
    async def evaluate(self, sig: Signal) -> None:
        if sig.id in self._evaluating or sig.status in ("confirmed", "secondary", "rejected"):
            return
        self._evaluating.add(sig.id)
        try:
            token = self.tokens.get(sig.token.key, sig.token)
            await self._enrich([token])
            self.history.record(token)
            sig.token = token
            sig.safety = await self.safety.evaluate(token)
            sig.updated_at = utcnow()
            age_min = (utcnow() - sig.created_at).total_seconds() / 60
            if sig.safety.hard_fail:
                sig.status = "rejected"
            elif sig.safety.passed:
                await self._promote(sig)
            elif age_min > self.s.recheck_minutes:
                sig.status = "rejected"
            else:
                sig.status = "watch"
            await self.storage.save_signal(sig)
            self.bus.publish(topics.SIGNAL, sig)
        finally:
            self._evaluating.discard(sig.id)

    async def _promote(self, sig: Signal) -> None:
        """Elige la moneda "ganadora" por noticia (la que concentra la liquidez) y alerta una sola vez."""
        async with self._promote_lock:
            key, liq = sig.token.key, sig.token.liquidity_usd or 0.0
            leader = self.leaders.get(sig.news.id)
            if await self.storage.was_alerted(key):
                sig.status, sig.is_leader = "confirmed", bool(leader and leader[0] == key)
                return
            takes_over = leader is not None and leader[0] != key and liq >= leader[1] * self.s.leader_switch_ratio
            if leader is None or takes_over:
                if takes_over:
                    prev = self.signals.get((leader[0], sig.news.id))
                    if prev:
                        prev.is_leader, prev.status = False, "secondary"
                        self.bus.publish(topics.SIGNAL, prev)
                self.leaders[sig.news.id] = (key, liq)
                sig.status, sig.is_leader = "confirmed", True
                if await self.storage.mark_alerted(sig):
                    sig.alerted = True
                    log.warning("🚨 SEÑAL %s ($%s) score=%.0f liq=%.0f", sig.token.name, sig.token.symbol,
                                sig.match_score, liq)
                    await self.alerts.send(sig, new_leader=takes_over)
            elif leader[0] == key:
                sig.status, sig.is_leader = "confirmed", True
            else:
                sig.status = "secondary"

    async def _enrich(self, tokens: list[TokenInfo]) -> None:
        if not self._dex:
            return
        now = time.monotonic()
        due = [t for t in tokens if now - self._last_enrich.get(t.key, 0) >= 5]
        by_chain: defaultdict[str, list[TokenInfo]] = defaultdict(list)
        for t in due:
            by_chain[t.chain].append(t)
            self._last_enrich[t.key] = now
        for chain, toks in by_chain.items():
            try:
                fresh = await self._dex.tokens(chain, [t.token_address for t in toks])
            except httpx.HTTPError as exc:
                log.info("Enriquecimiento DexScreener falló: %s", exc)
                continue
            fresh_by_key = {f.key: f for f in fresh}
            for t in toks:
                f = fresh_by_key.get(t.key)
                if f and t.merge_metrics(f):
                    self.bus.publish(topics.TOKEN_UPDATE, t)

    async def _recheck_loop(self) -> None:
        while True:
            await asyncio.sleep(self.s.recheck_seconds)
            watch = [s for s in self.signals.values() if s.status == "watch"]
            if not watch:
                continue
            await self._enrich([self.tokens.get(s.token.key, s.token) for s in watch])
            await asyncio.gather(*(self.evaluate(s) for s in watch), return_exceptions=True)

    # ------------------------------------------------------------------ utilidades
    def _signals_for(self, token_key: str, statuses: tuple[str, ...]) -> list[Signal]:
        return [s for (k, _), s in self.signals.items() if k == token_key and s.status in statuses]

    def _prune(self) -> None:
        cutoff = utcnow() - timedelta(hours=self.s.news_window_hours)
        for nid in [nid for nid, n in self.news.items() if n.received_at < cutoff]:
            del self.news[nid]
            self.leaders.pop(nid, None)
        for k in [k for k, s in self.signals.items() if s.news.id not in self.news]:
            del self.signals[k]
        tok_cutoff = utcnow() - timedelta(minutes=self.s.token_max_age_minutes)
        for k in [k for k, t in self.tokens.items() if t.first_seen < tok_cutoff]:
            del self.tokens[k]
