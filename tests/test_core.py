from __future__ import annotations

import json
from datetime import timedelta
from types import SimpleNamespace

import pytest

from radar.alerts.formatter import discord_payload, telegram_keyboard, telegram_text
from radar.bus import EventBus
from radar.config import Settings
from radar.fomo.base import JsonHttpTokenSource, normalize_token
from radar.llm import LLMClient
from radar.matcher.engine import MatchEngine
from radar.matcher.fuzzy import fuzzy_score
from radar.matcher.safety import MetricsHistory, SafetyFilter
from radar.models import NewsItem, Signal, TokenInfo
from radar.news.extractor import heuristic_extract
from radar.onchain.security import SecurityResult
from radar.storage import Storage
from radar.utils import headline_hash, to_datetime, utcnow

TPL = "https://fomo.family/tokens/{chain}/{address}"


def make_news(headline: str, **kw) -> NewsItem:
    data = heuristic_extract(headline)
    return NewsItem(id=headline_hash(headline), headline=headline, source="test", published_at=utcnow(),
                    keywords=data["keywords"], potential_tickers=kw.get("tickers", data["potential_tickers"]))


def make_token(sym: str, name: str, liq: float = 50_000, vol: float = 10_000, **kw) -> TokenInfo:
    return TokenInfo(name=name, symbol=sym, token_address=kw.get("addr", f"{sym}111111111111111111111111pump"),
                     chain="solana", liquidity_usd=liq, volume_5m=vol, market_cap=liq * 5,
                     buys_5m=kw.get("buys", 50), sells_5m=kw.get("sells", 20), fomo_url=TPL.format(chain="solana", address=sym),
                     dexscreener_url="https://dexscreener.com/solana/x")


# ---------------------------------------------------------------- extracción
def test_heuristic_extracts_penny_tickers():
    d = heuristic_extract("Trump announces new '1 cent' coin called the Trump Penny")
    assert {"PENNY", "1CENT", "TRUMP"} <= set(d["potential_tickers"])
    assert "Trump" in d["keywords"]


def test_heuristic_spanish_and_cashtag():
    assert "1CENT" in heuristic_extract("Trump anuncia moneda de 1 centavo")["potential_tickers"]
    assert heuristic_extract("Binance lists $PNUT")["potential_tickers"][0] == "PNUT"


def test_dates():
    assert to_datetime(1_700_000_000_000) == to_datetime(1_700_000_000)
    assert to_datetime("20261016T110000Z").hour == 11
    assert to_datetime("2026-10-16T11:00:00Z").tzinfo is not None


# ---------------------------------------------------------------- fuzzy
def test_fuzzy_exact_ticker():
    n = make_news("Trump announces new '1 cent' coin called the Trump Penny")
    assert fuzzy_score(make_token("PENNY", "Trump Penny"), n)[0] == 100


def test_fuzzy_single_generic_word_is_weak():
    n = make_news("Trump meets Xi Jinping in Beijing to discuss tariffs")
    score, _ = fuzzy_score(make_token("MAGA", "Trump Frog Coin"), n)
    assert score < 80


def test_fuzzy_unrelated():
    n = make_news("Fed's Waller says rate cuts remain on the table")
    assert fuzzy_score(make_token("PENNY", "Trump Penny"), n)[0] < 55


# ---------------------------------------------------------------- normalización JSON desconocido
def test_normalize_autodetect_nested():
    payload = {"data": {"page": 1, "items": [
        {"mint": "So1aNa111", "ticker": "$PENNY", "tokenName": "Trump Penny", "marketCap": "120000",
         "liquidity": {"usd": 30000}, "holders": 321, "createdAt": "2026-10-16T10:00:00Z"},
        {"tokenName": "sin address"},
    ]}}
    src = JsonHttpTokenSource(name="fomo:A", url="x", url_template=TPL)
    toks = src.parse_payload(payload)
    assert len(toks) == 1
    t = toks[0]
    assert (t.symbol, t.token_address, t.liquidity_usd, t.holder_count, t.on_fomo) == ("PENNY", "So1aNa111", 30000, 321, True)
    assert t.fomo_url == "https://fomo.family/tokens/solana/So1aNa111"


def test_normalize_with_field_map_and_evm():
    item = {"t": {"s": "CENT", "n": "Cent", "a": "0xAbC"}, "stats": {"liq": 9000}}
    fm = {"symbol": "t.s", "name": "t.n", "token_address": "t.a", "liquidity_usd": "stats.liq"}
    t = normalize_token(item, fm, source="x", default_chain="solana", url_template=TPL, on_fomo=True)
    assert t and t.chain == "base" and t.key == "base:0xabc" and t.liquidity_usd == 9000


# ---------------------------------------------------------------- seguridad
class FakeSecurity:
    def __init__(self, verdict="ok"):
        self.verdict = verdict

    async def check(self, token):
        return SecurityResult(self.verdict, ["x"], holders=777)


@pytest.mark.asyncio
async def test_safety_liquidity_and_honeypot():
    s = Settings()
    f = SafetyFilter(s, MetricsHistory(), FakeSecurity("ok"))
    r = await f.evaluate(make_token("A", "A", liq=1000))
    assert not r.passed and not r.liquidity_ok
    r = await f.evaluate(tok := make_token("B", "B"))
    assert r.passed and tok.holder_count == 777
    r = await SafetyFilter(s, MetricsHistory(), FakeSecurity("danger")).evaluate(make_token("C", "C"))
    assert r.hard_fail and not r.passed


@pytest.mark.asyncio
async def test_safety_requires_growth_without_buy_pressure():
    s = Settings()
    r = await SafetyFilter(s, MetricsHistory(), FakeSecurity()).evaluate(make_token("D", "D", buys=5, sells=40))
    assert not r.passed and r.volume_trend == "unknown"


def test_volume_trend():
    h = MetricsHistory()
    t = make_token("E", "E", vol=1000)
    h.record(t)
    h._h[t.key][0].t -= 30  # simula snapshot de hace 30s
    t.volume_5m = 1500
    h.record(t)
    assert h.trend(t.key, 0) == "up"


# ---------------------------------------------------------------- alertas
def test_formatters():
    sig = Signal(news=make_news("Trump announces <1 cent> coin"), token=make_token("PENNY", "Trump Penny"),
                 fuzzy_score=100, llm_score=95, match_score=96.5, reason="referencia directa")
    txt = telegram_text(sig)
    assert "&lt;1 cent&gt;" in txt and "<code>" in txt and "$PENNY" in txt
    kb = telegram_keyboard(sig)["inline_keyboard"]
    assert kb[0][0]["url"].startswith("https://fomo.family/") and kb[1][0]["copy_text"]["text"] == sig.token.token_address
    d = discord_payload(sig, "@here")
    assert d["content"].startswith("@here") and all(f["value"] for f in d["embeds"][0]["fields"])


# ---------------------------------------------------------------- LLM (SDK simulado)
@pytest.mark.asyncio
async def test_llm_judge_parses_structured_output(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    llm = LLMClient(Settings())
    captured = {}

    async def fake_create(**kwargs):
        captured.update(kwargs)
        body = {"results": [{"index": 0, "score": 140, "reason": "ok"}, {"index": 9, "score": 50, "reason": "x"}]}
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps(body))])

    monkeypatch.setattr(llm._client.messages, "create", fake_create)
    out = await llm.judge("Trump Penny", "PENNY", ["h0", "h1"])
    assert out == [(100, "ok"), (0, "sin respuesta")]
    assert captured["model"] == "claude-haiku-5-5"
    assert captured["output_config"]["format"]["type"] == "json_schema"


# ---------------------------------------------------------------- motor completo
class FakeLLM:
    model = "fake"

    def __init__(self):
        self.calls = 0

    async def judge(self, name, symbol, headlines, description="", links=None):
        self.calls += 1
        return [(95 if "Penny" in h else 10, "fake") for h in headlines]


class CollectAlerts:
    def __init__(self):
        self.sent: list[tuple[str, bool]] = []

    async def send(self, signal, *, new_leader=False):
        self.sent.append((signal.token.symbol, new_leader))


@pytest.mark.asyncio
async def test_engine_leader_selection_and_dedupe():
    s = Settings(demo_mode=True)
    storage = Storage(":memory:")
    await storage.init()
    history = MetricsHistory()
    alerts = CollectAlerts()
    llm = FakeLLM()
    eng = MatchEngine(s, EventBus(), storage, llm, SafetyFilter(s, history, FakeSecurity()), history, alerts)

    news = make_news("Trump announces new '1 cent' coin called the Trump Penny")
    other = make_news("Ethereum ETF sees record inflows")
    await eng.on_news(news)
    await eng.on_news(other)

    junk = make_token("TPENNY", "Trump Penny Official", liq=1000)       # match pero sin liquidez -> watch
    winner = make_token("PENNY", "Trump Penny", liq=40_000)             # primer líder -> alerta
    small = make_token("PENNYX", "Penny Trump X", liq=50_000)           # no duplica liquidez -> secundaria
    whale = make_token("PENNY2", "Trump Penny 2", liq=200_000)          # >= 2x -> nuevo líder
    for t in (junk, winner, small, whale):
        await eng.on_token(t)
        await _drain(eng)
    # mismo token visto otra vez: nunca re-alerta
    await eng.on_token(winner)
    await _drain(eng)

    status = {k[0].split(":")[1][:6]: v.status for k, v in eng.signals.items()}
    assert alerts.sent == [("PENNY", False), ("PENNY2", True)]
    assert eng.signals[(junk.key, news.id)].status == "watch"
    assert eng.signals[(small.key, news.id)].status == "secondary"
    assert eng.signals[(winner.key, news.id)].status == "secondary"  # destronado
    assert all(k[1] == news.id for k in eng.signals), status        # la noticia de ETH no matchea
    await storage.close()


async def _drain(eng: MatchEngine) -> None:
    import asyncio

    while eng._tasks:
        await asyncio.gather(*list(eng._tasks), return_exceptions=True)


def test_token_age_filter_cutoff():
    t = make_token("OLD", "Old")
    t.created_at = utcnow() - timedelta(days=3)
    assert (utcnow() - t.created_at) > timedelta(minutes=Settings().token_max_age_minutes)
