"""Modo replay: reproduce un caso (noticias + tokens + métricas en el tiempo) a velocidad acelerada
por EL MISMO pipeline real (extractor, búsqueda inversa, narrativas, matcher, filtros, alertas) y
reporta qué habría alertado el radar y cuándo.

    python -m radar --replay scenarios/dacity_gta6.json              # con dashboard en vivo
    python -m radar --replay scenarios/dacity_gta6.json --headless   # solo reporte en consola

Formato del escenario (tiempos `t` en minutos relativos a la noticia principal, t=0):
{
  "name", "description", "sources": [...],
  "news":   [{"t": 0, "headline": "...", "source": "...", "url": "..."}],
  "tokens": [{"name", "symbol", "address", "chain": "solana", "created_min": -7200,
              "honeypot": false, "description", "twitter",
              "timeline": [{"t": -30, "market_cap": 104000, "liquidity_usd": 25000, "volume_5m": 300,
                            "buys_5m": 4, "sells_5m": 3, "holder_count": 150}, ...]}],
  "expect": {"first_alert": "<address>", "within_min": 15, "never_alert": ["<address>"]}
}
Un token con created_min anterior al inicio del replay "ya existía": no aparece en el stream de
tokens nuevos y solo puede encontrarse con la búsqueda inversa (como pasa en la realidad).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any, AsyncIterator

import httpx

from . import bus as topics
from .config import Settings
from .fomo.base import TokenSource
from .models import TokenInfo
from .news.base import RawNews, StreamingNewsSource
from .onchain.security import SecurityResult
from .utils import utcnow

log = logging.getLogger(__name__)
METRICS = ("market_cap", "liquidity_usd", "volume_5m", "buys_5m", "sells_5m", "holder_count",
           "fomo_buys_5m", "fomo_buyers_5m", "fomo_volume_usd_5m")


@dataclass
class ReplayClock:
    speed: float  # minutos de escenario por minuto real
    start_min: float  # minuto de escenario donde arranca el replay
    wall0: float = field(default_factory=time.monotonic)

    def now_min(self) -> float:
        return self.start_min + (time.monotonic() - self.wall0) * self.speed / 60

    async def sleep_until(self, t_min: float) -> None:
        delay = (t_min - self.now_min()) * 60 / self.speed
        if delay > 0:
            await asyncio.sleep(delay)


class Scenario:
    def __init__(self, data: dict[str, Any], no_news: bool = False) -> None:
        self.data = data
        self.name = data.get("name", "escenario") + (" (SIN noticias)" if no_news else "")
        self.news = [] if no_news else sorted(data.get("news", []), key=lambda n: n["t"])
        self.tokens = data.get("tokens", [])
        self.expect = data.get("expect", {})
        times = [n["t"] for n in self.news] + [p["t"] for tk in self.tokens for p in tk.get("timeline", [])]
        self.start = min(min(times) if times else 0, 0) - 1
        self.end = max(times) if times else 0

    @classmethod
    def load(cls, path: str | Path) -> "Scenario":
        """`ruta.json` o `ruta.json#sin-noticias` (borra las noticias: prueba narrativas + búsqueda)."""
        path, _, flag = str(path).partition("#")
        return cls(json.loads(Path(path).read_text(encoding="utf-8")), no_news=flag == "sin-noticias")

    def by_address(self, address: str) -> dict | None:
        return next((t for t in self.tokens if t["address"] == address), None)


def _metrics_at(tk: dict, t: float) -> dict:
    point: dict = {}
    for p in sorted(tk.get("timeline", []), key=lambda p: p["t"]):
        if p["t"] <= t:
            point = p
    return {k: point.get(k) for k in METRICS}


def _token(tk: dict, t: float, clock: ReplayClock, template: str) -> TokenInfo:
    chain = tk.get("chain", "solana")
    age_min = t - tk.get("created_min", t)  # la edad NO se comprime: importa para los filtros
    return TokenInfo(
        name=tk["name"], symbol=tk["symbol"], token_address=tk["address"], chain=chain,
        created_at=utcnow() - timedelta(minutes=max(age_min, 0)),
        description=tk.get("description"), twitter=tk.get("twitter"), website=tk.get("website"),
        fomo_url=template.format(chain=chain, address=tk["address"]),
        dexscreener_url=f"https://dexscreener.com/{chain}/{tk['address']}",
        source="replay", on_fomo=bool(tk.get("on_fomo", True)), **_metrics_at(tk, t))


class ReplayNewsSource(StreamingNewsSource):
    name = "replay-news"

    def __init__(self, sc: Scenario, clock: ReplayClock) -> None:
        self.sc, self.clock = sc, clock

    async def stream(self) -> AsyncIterator[RawNews]:
        for n in self.sc.news:
            await self.clock.sleep_until(n["t"])
            yield RawNews(headline=n["headline"], source=n.get("source", "replay"), url=n.get("url"),
                          summary=n.get("summary", ""), published_at=utcnow())
        while True:
            await asyncio.sleep(3600)


class ReplayTokenSource(TokenSource):
    """Stream de tokens nuevos (lanzamientos dentro de la ventana) + actualizaciones de métricas."""

    name = "replay-tokens"
    on_fomo = True

    def __init__(self, sc: Scenario, clock: ReplayClock, template: str) -> None:
        self.sc, self.clock, self.template = sc, clock, template

    async def poll(self, client: httpx.AsyncClient) -> list[TokenInfo]:
        return []

    def stream(self) -> AsyncIterator[TokenInfo]:
        return self._run()

    async def _run(self) -> AsyncIterator[TokenInfo]:
        events: list[tuple[float, dict]] = []
        for tk in self.sc.tokens:
            if tk.get("created_min", -1e9) >= self.sc.start:
                events.append((tk["created_min"], tk))  # lanzamiento visible para el stream
            for p in tk.get("timeline", []):
                events.append((p["t"], tk))  # actualización de métricas (el scanner la ignora si no lo sigue)
        for t, tk in sorted(events, key=lambda e: e[0]):
            await self.clock.sleep_until(t)
            yield _token(tk, t, self.clock, self.template)
        while True:
            await asyncio.sleep(3600)


class ReplaySearch:
    """Simula la búsqueda de DexScreener: devuelve los tokens ya creados cuyo nombre/ticker contiene la query."""

    def __init__(self, sc: Scenario, clock: ReplayClock, template: str) -> None:
        self.sc, self.clock, self.template = sc, clock, template
        self.queries: list[str] = []

    async def search(self, query: str) -> list[TokenInfo]:
        self.queries.append(query)
        t, q = self.clock.now_min(), query.lower().replace(" ", "")
        return [_token(tk, t, self.clock, self.template) for tk in self.sc.tokens
                if tk.get("created_min", -1e9) <= t
                and (q in tk["symbol"].lower() or q in tk["name"].lower().replace(" ", ""))]


class ReplaySecurity:
    def __init__(self, sc: Scenario) -> None:
        self.sc = sc

    async def check(self, token: TokenInfo) -> SecurityResult:
        tk = self.sc.by_address(token.token_address) or {}
        if tk.get("honeypot"):
            return SecurityResult("danger", ["⛔ honeypot (escenario)"], provider="replay")
        return SecurityResult("ok", ["sin riesgos (escenario)"], provider="replay")


def scale_settings(s: Settings, speed: float) -> None:
    """Las ventanas internas del radar son de reloj real: se comprimen al ritmo del replay."""
    k = 60 / speed  # segundos reales por minuto de escenario
    s.recheck_seconds = max(15 / 60 * k, 0.05)
    s.trend_min_gap_s = max(10 / 60 * k, 0.02)
    s.narrative_window_min *= 1 / speed
    s.narrative_baseline_hours *= 1 / speed
    s.narrative_cooldown_min *= 1 / speed
    s.narrative_warmup_min = 0
    s.news_max_age_minutes = 10**6
    s.db_path = ":memory:"


# ---------------------------------------------------------------------------- reporte
class ReplayRecorder:
    def __init__(self, sc: Scenario, clock: ReplayClock) -> None:
        self.sc, self.clock = sc, clock
        self.lines: list[tuple[float, str]] = []
        self.alerts: list[tuple[float, Any]] = []
        self._seen_status: dict[str, str] = {}

    async def run(self, bus) -> None:
        q = bus.subscribe()
        while True:
            topic, p = await q.get()
            t = self.clock.now_min()
            if topic == topics.NEWS:
                self.lines.append((t, f"📰 {p.headline[:90]}  [tickers: {', '.join(p.potential_tickers[:6])}]"))
            elif topic == topics.TOKEN:
                self.lines.append((t, f"🪙 token visto: {p.name} (${p.symbol}) vía {p.source}"))
            elif topic == topics.SIGNAL and self._seen_status.get(p.id) != p.status:
                self._seen_status[p.id] = p.status
                reasons = "; ".join((p.safety.reasons if p.safety else [])[:2])
                self.lines.append((t, f"🎯 {p.status.upper():10} ${p.token.symbol:<10} score={p.match_score:.0f} "
                                      f"liq={p.token.liquidity_usd or 0:,.0f}" + (f"  ({reasons})" if reasons else "")))
                if p.alerted and not any(a[1].id == p.id for a in self.alerts):
                    self.alerts.append((t, p))
                    self.lines.append((t, f"🚨 ALERTA → {p.token.name} (${p.token.symbol}) {p.token.fomo_url}"))

    def report(self) -> tuple[str, bool]:
        out = [f"\n══ Replay: {self.sc.name} ══"]
        for t, line in sorted(self.lines, key=lambda x: x[0]):
            out.append(f"[{t:+7.1f} min] {line}")
        exp = dict(self.sc.expect)
        if not self.sc.news and "first_alert_without_news" in exp:
            exp.update(first_alert=exp["first_alert_without_news"], within_min=exp.get("within_min_without_news"))
        ok = True
        out.append("\n── Resultado ──")
        if not self.alerts:
            out.append("✖ el radar no emitió ninguna alerta")
            ok = not exp.get("first_alert")
        else:
            t, first = self.alerts[0]
            out.append(f"Primera alerta: {first.token.name} (${first.token.symbol}) a los {t:+.1f} min de la noticia")
            if exp.get("first_alert"):
                hit = first.token.token_address == exp["first_alert"]
                in_time = t <= exp.get("within_min", 1e9)
                ok = hit and in_time
                out.append(f"{'✔' if hit else '✖'} token esperado: {exp['first_alert']}")
                out.append(f"{'✔' if in_time else '✖'} dentro de {exp.get('within_min')} min")
        bad = [a for _, a in self.alerts if a.token.token_address in set(exp.get("never_alert", []))]
        for a in bad:
            out.append(f"✖ alertó un token que NO debía: ${a.token.symbol}")
        ok = ok and not bad
        out.append("PASA ✅" if ok else "FALLA ❌")
        return "\n".join(out), ok


async def run_headless(settings: Settings, path: str, speed: float, tail_min: float = 10.0) -> tuple[str, bool]:
    from .app import Radar

    sc = Scenario.load(path)
    settings.replay_path = path
    settings.replay_speed = speed
    scale_settings(settings, speed)
    radar = Radar(settings)
    await radar.start()
    recorder = ReplayRecorder(sc, radar.replay_clock)
    rec_task = asyncio.create_task(recorder.run(radar.bus))
    try:
        await radar.replay_clock.sleep_until(sc.end + tail_min)
        await asyncio.sleep(0.3)
    finally:
        rec_task.cancel()
        await radar.stop()
    return recorder.report()
