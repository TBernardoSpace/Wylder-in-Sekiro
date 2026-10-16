from __future__ import annotations

import asyncio
import base64
import json
import struct
import time

import pytest

from radar.bus import EventBus
from radar.config import Settings
from radar.fomo.via_c_pumpfun_stream import PumpFunStreamSource
from radar.matcher.engine import MatchEngine
from radar.matcher.fuzzy import fuzzy_score
from radar.matcher.narratives import NarrativeDetector
from radar.matcher.safety import MetricsHistory, SafetyFilter
from radar.models import NewsItem, TokenInfo
from radar.news.extractor import heuristic_extract
from radar.onchain import pumpfun
from radar.onchain.pumpfun import (
    CREATE_DISC, TRADE_DISC, PumpCreate, PumpMetadata, PumpTrade, b58encode, events_from_logs,
    parse_metadata, price_sol,
)
from radar.onchain.security import SecurityResult
from radar.storage import Storage
from radar.utils import headline_hash, utcnow

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
MINT = "2bCCHQadi4QeHWPKG3w13siCyWWQPgkFtniouTqVpump"
USER = b58encode(bytes(range(1, 33)))


def b58decode(s: str) -> bytes:
    n = 0
    for c in s:
        n = n * 58 + _B58.index(c)
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\0" * (len(s) - len(s.lstrip("1"))) + raw


def pk(s: str) -> bytes:
    b = b58decode(s)
    assert len(b) <= 32, s
    return b"\0" * (32 - len(b)) + b


def bstr(s: str) -> bytes:
    raw = s.encode()
    return struct.pack("<I", len(raw)) + raw


def create_log(name="Trump Dollar", symbol="TDOLLAR", uri="https://ipfs.io/ipfs/QmTestCid1234567890123456789012345678901234567"):
    # Orden de campos del IDL oficial de pump.fun (CreateEvent)
    data = (CREATE_DISC + bstr(name) + bstr(symbol) + bstr(uri) + pk(MINT) + pk(USER) + pk(USER) + pk(USER)
            + struct.pack("<q", int(time.time()))
            + struct.pack("<QQQQ", 1_073_000_000_000_000, 30_000_000_000, 793_100_000_000_000, 1_000_000_000_000_000))
    return "Program data: " + base64.b64encode(data).decode()


def trade_log(sol: float, is_buy: bool, user: str = USER, vsol=40_000_000_000, vtok=800_000_000_000_000, rsol=10_000_000_000):
    data = (TRADE_DISC + pk(MINT) + struct.pack("<QQ", int(sol * 1e9), 1_000_000) + bytes([is_buy]) + pk(user)
            + struct.pack("<q", int(time.time())) + struct.pack("<QQQQ", vsol, vtok, rsol, 700_000_000_000_000))
    return "Program data: " + base64.b64encode(data).decode()


# ---------------------------------------------------------------- decodificación (IDL oficial)
def test_b58_roundtrip():
    assert b58encode(pk(MINT)) == MINT
    assert b58encode(b"\0" * 32) == "1" * 32


def test_decode_create_and_trade_from_logs():
    logs = ["Program 6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P invoke [1]", "Program log: Instruction: Create",
            create_log(), trade_log(1.5, True), "Program data: !!no-base64!!"]
    create, trade = events_from_logs(logs)
    assert isinstance(create, PumpCreate) and (create.name, create.symbol, create.mint) == ("Trump Dollar", "TDOLLAR", MINT)
    assert create.token_total_supply == 1_000_000_000_000_000
    assert isinstance(trade, PumpTrade) and trade.is_buy and trade.sol_amount == 1_500_000_000 and trade.user == USER
    assert price_sol(30_000_000_000, 1_073_000_000_000_000) == pytest.approx(2.796e-8, rel=1e-3)


def test_parse_metadata():
    m = parse_metadata({"name": "x", "description": "Trump on the new $1 coin", "twitter": "https://x.com/WatcherGuru/status/1",
                        "extensions": {"website": "https://usmint.gov"}})
    assert (m.description, m.twitter, m.website) == ("Trump on the new $1 coin", "https://x.com/WatcherGuru/status/1", "https://usmint.gov")
    assert pumpfun.gateway_url("ipfs://QmAbcdefghijklmnopqrstuvwxyz0123456789ABCDEFGH", "https://gw/ipfs/") == \
        "https://gw/ipfs/QmAbcdefghijklmnopqrstuvwxyz0123456789ABCDEFGH"


# ---------------------------------------------------------------- stream (RPC) -> métricas de la curva
@pytest.mark.asyncio
async def test_stream_rpc_metrics(monkeypatch):
    async def fake_fetch(self, uri):
        return PumpMetadata(description="Trump appears on the new $1 coin", twitter="https://x.com/WatcherGuru/status/42")

    monkeypatch.setattr(pumpfun.MetadataFetcher, "fetch", fake_fetch)
    s = Settings(pumpfun_stream="rpc", solana_rpc_wss="ws://unused", sol_price_usd=200)
    src = PumpFunStreamSource(s)
    import httpx

    src._meta = pumpfun.MetadataFetcher(httpx.AsyncClient(), "")
    note = lambda logs: {"params": {"result": {"value": {"err": None, "logs": logs}}}}  # noqa: E731
    src.on_message(note([create_log()]))
    for i in range(3):
        src.on_message(note([trade_log(2.0, True, user=b58encode(bytes([i + 50]) * 32))]))
    src.on_message(note([trade_log(1.0, False)]))
    await asyncio.sleep(0.05)
    first, with_meta = src._out.get_nowait(), src._out.get_nowait()
    assert (first.symbol, first.source, first.fomo_url) == ("TDOLLAR", "pumpfun:stream", f"https://fomo.family/tokens/solana/{MINT}")
    assert with_meta.description.startswith("Trump") and with_meta.twitter.endswith("/42")
    snap = src.snapshot(MINT, time.time())
    assert (snap.buys_5m, snap.sells_5m, snap.volume_5m) == (3, 1, 1400.0)
    assert snap.liquidity_usd == 4000.0  # 10 SOL reales x 2 x $200
    assert snap.market_cap == pytest.approx(0.05 / 1000 * 1e9 * 200 / 1000, rel=1e-6)


@pytest.mark.asyncio
async def test_stream_pumpportal_end_to_end(monkeypatch):
    import websockets

    async def fake_fetch(self, uri):
        return None

    monkeypatch.setattr(pumpfun.MetadataFetcher, "fetch", fake_fetch)

    async def server(ws):
        assert json.loads(await ws.recv()) == {"method": "subscribeNewToken"}
        await ws.send(json.dumps({"message": "Successfully subscribed to token creation events."}))
        await ws.send(json.dumps({"signature": "s", "mint": MINT, "traderPublicKey": USER, "txType": "create",
                                  "initialBuy": 1e7, "bondingCurveKey": "x", "vTokensInBondingCurve": 1e9,
                                  "vSolInBondingCurve": 30, "marketCapSol": 30.5, "name": "Trump Dollar",
                                  "symbol": "TDOLLAR", "uri": "https://ipfs.io/ipfs/x"}))
        await ws.wait_closed()

    async with websockets.serve(server, "127.0.0.1", 0) as srv:
        port = srv.sockets[0].getsockname()[1]
        src = PumpFunStreamSource(Settings(pumpportal_wss=f"ws://127.0.0.1:{port}", sol_price_usd=200,
                                           pumpfun_coin_url=""))
        stream = src.stream()
        tok = await asyncio.wait_for(anext(stream), timeout=10)
        await stream.aclose()
    assert (tok.symbol, tok.token_address, tok.creator, tok.market_cap) == ("TDOLLAR", MINT, USER, 6100.0)


# ---------------------------------------------------------------- cruce con descripción y links
def news(headline: str, url: str | None = None) -> NewsItem:
    d = heuristic_extract(headline)
    return NewsItem(id=headline_hash(headline), headline=headline, source="t", url=url, published_at=utcnow(),
                    keywords=d["keywords"], potential_tickers=d["potential_tickers"])


def token(sym: str, name: str, liq: float = 50_000, **kw) -> TokenInfo:
    return TokenInfo(name=name, symbol=sym, token_address=kw.pop("addr", f"{sym}1111111111111111111111pump"),
                     chain="solana", liquidity_usd=liq, volume_5m=10_000, buys_5m=40, sells_5m=10, **kw)


def test_fuzzy_uses_description_and_links():
    n = news("Treasury Secretary Bessent unveils final design of $1 coin featuring Trump",
             url="https://x.com/WatcherGuru/status/1977000000000000000?s=20")
    plain = token("GOLDY", "Goldy")
    assert fuzzy_score(plain, n)[0] < 55
    described = token("GOLDY", "Goldy", description="The official meme of Bessent's new $1 coin design with Trump")
    assert fuzzy_score(described, n)[0] >= 70
    linked = token("GOLDY", "Goldy", twitter="https://twitter.com/WatcherGuru/status/1977000000000000000")
    assert fuzzy_score(linked, n) == (100.0, "el token enlaza directamente la noticia/tweet")


@pytest.mark.parametrize("sym,name", [("TRUMPDOLLAR", "Trump Dollar"), ("1DOLLAR", "One Dollar Trump"),
                                       ("TDOLLAR", "Trump Dollar Coin")])
def test_trump_one_dollar_case(sym, name):
    """Caso del usuario (moneda de 1 dólar con Trump). Titulares representativos: no conocemos el original."""
    for h in ("Treasury Secretary Bessent unveils final design of $1 coin featuring Trump",
              "Bessent presenta el diseño final de la moneda de 1 dólar con la cara de Trump"):
        assert fuzzy_score(token(sym, name), news(h))[0] >= 80, (sym, h)


# ---------------------------------------------------------------- narrativas emergentes
def test_narrative_spike_detected_with_most_cited_link():
    s = Settings(narrative_warmup_min=0)
    det = NarrativeDetector(s, EventBus(), Storage(":memory:"))
    det.started -= 6 * 3600
    t0 = time.time()
    # línea base: "trump" aparece siempre (60 tokens en 6 h) -> no debe disparar por 4 tokens nuevos
    for i in range(60):
        det.observe(token(f"T{i}", f"Trump Cat {i}"), t0 - 6 * 3600 + i * 300)
    tweet = "https://x.com/WatcherGuru/status/1977000000000000000"
    items = []
    for i, (sym, name) in enumerate([("TDOLLAR", "Trump Dollar"), ("1DOLLAR", "One Dollar"), ("DOLLAR", "Dollar Coin"),
                                     ("TRUMP1", "Trump Dollar Face")]):
        items += det.observe(token(sym, name, twitter=tweet if i < 2 else None), t0 + i * 30)
    assert len(items) == 1
    item = items[0]
    assert item.extractor == "cluster" and item.keywords[0] == "dollar" and item.url == tweet
    assert {"TDOLLAR", "1DOLLAR"} <= set(item.potential_tickers)
    assert "trump" not in [i.keywords[0] for i in items]
    assert det.observe(token("DOLLAR5", "Dollar Five"), t0 + 200) == []  # cooldown


@pytest.mark.asyncio
async def test_engine_alerts_leader_of_emergent_narrative():
    s = Settings(demo_mode=True, narrative_warmup_min=0)
    storage = Storage(":memory:")
    await storage.init()
    history = MetricsHistory()
    sent = []

    class Alerts:
        async def send(self, signal, *, new_leader=False):
            sent.append(signal.token.symbol)

    class Sec:
        async def check(self, t):
            return SecurityResult("ok", [])

    class NoLLM:  # si el juez se llamara para la narrativa sintética, rompería el test
        async def judge(self, *a, **k):
            raise AssertionError("no debe juzgar narrativas sintéticas")

    eng = MatchEngine(s, EventBus(), storage, NoLLM(), SafetyFilter(s, history, Sec()), history, Alerts())
    det = NarrativeDetector(s, EventBus(), storage)
    det.started -= 3600
    toks = [token("TDOLLAR", "Trump Dollar", liq=3000), token("1DOLLAR", "One Dollar", liq=90_000),
            token("DOLLARX", "Dollar X", liq=12_000), token("BDOLLAR", "Big Dollar", liq=8000)]
    items = []
    for t in toks:
        await eng.on_token(t)
        items += det.observe(t, time.time())
    assert len(items) == 1
    await eng.on_news(items[0])
    while eng._tasks:
        await asyncio.gather(*list(eng._tasks), return_exceptions=True)
    assert sent == ["1DOLLAR"]  # el de más liquidez alerta primero; el resto no lo duplica (2x)
    assert eng.signals[(toks[0].key, items[0].id)].status == "watch"  # sin liquidez suficiente
    await storage.close()


def test_amounts_are_not_dollar_coins():
    """Regresión: "$1.2B" es un monto, no la moneda de 1 dólar."""
    n = news("Ethereum ETF sees record $1.2B daily inflows")
    assert "1DOLLAR" not in n.potential_tickers and "12DOLLAR" not in n.potential_tickers
    for sym, name in [("TDOLLAR", "Trump Dollar"), ("1DOLLAR", "One Dollar Trump"), ("MDOLLAR", "Mint Dollar Coin")]:
        assert fuzzy_score(token(sym, name), n)[0] < 80


def test_short_tickers_need_exact_match():
    from radar.matcher.fuzzy import _ticker_sim

    assert _ticker_sim("TDOLLAR", "TDOLLAR") == 100
    assert _ticker_sim("EDOLLAR", "TDOLLAR") <= 75
    assert _ticker_sim("1DOLLAR", "12DOLLAR") <= 60
    assert _ticker_sim("TRUMPPENNY", "TRUMPPENNYS") > 90
