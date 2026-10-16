"""Detector de narrativas emergentes: encuentra "la noticia" sin conocer el titular.

Cuando sale algo viral, en minutos aparecen muchos tokens con el mismo tema ("dollar", "penny"...).
Los creadores de memecoins suelen ser más rápidos que los feeds de noticias. Este módulo:

1. Cuenta, por término significativo del nombre/ticker, cuántos tokens NUEVOS lo usan en los
   últimos NARRATIVE_WINDOW_MIN minutos.
2. Lo compara con la tasa normal de ese término (línea base de NARRATIVE_BASELINE_HOURS), para que
   "trump" o "pepe", que aparecen siempre, no disparen por sí solos.
3. Si hay un pico (>= NARRATIVE_MIN_TOKENS tokens y >= NARRATIVE_SPIKE_RATIO veces lo normal),
   publica una noticia sintética "Narrativa emergente". Su link es el tweet o la web que más
   enlazan esos tokens, que suele ser la noticia original.

El motor la trata como cualquier noticia: cruza los tokens del clúster, aplica los filtros de
seguridad y alerta solo al líder (el que concentra la liquidez).
"""
from __future__ import annotations

import logging
import time
from collections import Counter, defaultdict, deque

from .. import bus as topics
from ..bus import EventBus
from ..config import Settings
from ..models import NewsItem, TokenInfo
from ..storage import Storage
from ..utils import headline_hash, norm_alnum, significant_words, to_datetime
from .fuzzy import _link_key

log = logging.getLogger(__name__)


def token_terms(token: TokenInfo) -> set[str]:
    terms = set(significant_words(token.name))
    sym = token.symbol.lower()
    if sym.isalpha() and len(sym) >= 3:
        terms.add(sym)
    return {t for t in terms if not t.isdigit()}


class NarrativeDetector:
    def __init__(self, settings: Settings, bus: EventBus, storage: Storage) -> None:
        self.s, self.bus, self.storage = settings, bus, storage
        self.started = time.time()
        self.hist: defaultdict[str, deque[tuple[float, str]]] = defaultdict(deque)
        self.tokens: dict[str, TokenInfo] = {}
        self.cooldown: dict[str, float] = {}
        self.recent_news_terms: deque[tuple[float, set[str]]] = deque()

    async def run(self) -> None:
        q = self.bus.subscribe()
        while True:
            topic, payload = await q.get()
            if topic == topics.TOKEN:
                for item in self.observe(payload, time.time()):
                    await self.storage.save_news(item)
                    log.warning("🌊 %s", item.headline)
                    self.bus.publish(topics.NEWS, item)
            elif topic == topics.TOKEN_UPDATE and payload.key in self.tokens:
                self.tokens[payload.key] = payload  # trae twitter/web para el link de la narrativa
            elif topic == topics.NEWS and payload.extractor != "cluster":
                words = set(significant_words(" ".join(payload.keywords) + " " + payload.headline))
                self.recent_news_terms.append((time.time(), words))

    def observe(self, token: TokenInfo, now: float) -> list[NewsItem]:
        terms = token_terms(token)
        if not terms:
            return []
        self.tokens[token.key] = token
        for t in terms:
            self.hist[t].append((now, token.key))
        self._prune(now)
        return [item for t in terms if (item := self._check(t, now))]

    def _check(self, term: str, now: float) -> NewsItem | None:
        s = self.s
        win = s.narrative_window_min * 60
        if now - self.started < s.narrative_warmup_min * 60 or self.cooldown.get(term, 0) > now:
            return None
        events = self.hist[term]
        current = {k for ts, k in events if ts >= now - win}
        if len(current) < s.narrative_min_tokens:
            return None
        base_events = sum(1 for ts, _ in events if ts < now - win)
        observed_min = max(min(now - self.started, s.narrative_baseline_hours * 3600) - win, 60) / 60
        expected = base_events / observed_min * s.narrative_window_min
        if len(current) < s.narrative_spike_ratio * max(expected, 1.0):
            return None
        # Si una noticia real ya cubre este término, el motor ya la está cruzando
        if any(term in words for ts, words in self.recent_news_terms if ts >= now - s.news_window_hours * 3600):
            self.cooldown[term] = now + s.narrative_cooldown_min * 60
            return None
        self.cooldown[term] = now + s.narrative_cooldown_min * 60
        return self._build(term, current, expected, now)

    def _build(self, term: str, keys: set[str], expected: float, now: float) -> NewsItem:
        toks = [self.tokens[k] for k in keys if k in self.tokens]
        co = Counter(t for tok in toks for t in token_terms(tok) if t != term)
        co_terms = [t for t, c in co.most_common(5) if c >= 2]
        keyed: Counter[str] = Counter()
        sample: dict[str, str] = {}
        for tok in toks:
            for x in {tok.twitter, tok.website} - {None}:
                if k := _link_key(x):
                    keyed[k] += 1
                    sample.setdefault(k, x)
        top = keyed.most_common(1)
        link = sample[top[0][0]] if top and top[0][1] >= 2 else None  # al menos 2 tokens citan lo mismo
        symbols = [norm_alnum(t.symbol) for t in sorted(toks, key=lambda t: -(t.liquidity_usd or 0))]
        mins = int(self.s.narrative_window_min)
        theme = " ".join([term] + co_terms[:2])
        headline = f"Narrativa emergente en memecoins: «{theme}» — {len(keys)} tokens nuevos en {mins} min"
        if expected >= 0.1:
            headline += f" (normal: {expected:.1f})"
        return NewsItem(
            id=headline_hash(f"narrative:{term}:{int(now // (self.s.narrative_cooldown_min * 60))}"),
            headline=headline,
            source="pump.fun · narrativa emergente" + (" (link más citado)" if link else ""),
            url=link,
            published_at=to_datetime(now),
            keywords=[term] + co_terms,
            potential_tickers=list(dict.fromkeys(s for s in symbols if s))[:15] + [term.upper()],
            narrative=theme,
            extractor="cluster",
        )

    def _prune(self, now: float) -> None:
        cutoff = now - self.s.narrative_baseline_hours * 3600
        if len(self.hist) > 50_000 or int(now) % 60 == 0:
            for term in list(self.hist):
                dq = self.hist[term]
                while dq and dq[0][0] < cutoff:
                    dq.popleft()
                if not dq:
                    del self.hist[term]
        while self.recent_news_terms and self.recent_news_terms[0][0] < now - self.s.news_window_hours * 3600:
            self.recent_news_terms.popleft()
        if len(self.tokens) > 30_000:
            for k in list(self.tokens)[:10_000]:
                del self.tokens[k]
