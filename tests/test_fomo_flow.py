from __future__ import annotations

import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fastapi.testclient import TestClient

from radar.config import Settings
from radar.fomo.base import JsonHttpTokenSource
from radar.fomo.via_d_flow import CO_SIGNER, FlowAggregator, FomoFlowSource, FomoTrade, parse_fomo_tx
from radar.models import TokenInfo
from radar.onchain.dexscreener import DexScreenerClient
from radar.utils import utcnow

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
MINT = "2bCCHQadi4QeHWPKG3w13siCyWWQPgkFtniouTqVpump"
USER = "UserWa11et1111111111111111111111111111111111"
TPL = "https://fomo.family/tokens/{chain}/{address}"


def bal(idx, mint, owner, amount):
    return {"accountIndex": idx, "mint": mint, "owner": owner, "uiTokenAmount": {"uiAmountString": str(amount)}}


def fomo_tx(sig="sig1", user=USER, pre=None, post=None, cosigner=True, err=None, lamports=(0, 0)):
    keys = [{"pubkey": CO_SIGNER, "signer": cosigner}, {"pubkey": user, "signer": True}]
    return {
        "blockTime": int(time.time()),
        "meta": {"err": err, "preTokenBalances": pre or [], "postTokenBalances": post or [],
                 "preBalances": [0, lamports[0]], "postBalances": [0, lamports[1]]},
        "transaction": {"signatures": [sig], "message": {"accountKeys": keys, "instructions": []}},
    }


def buy_tx(sig="sig1", user=USER, usd=50.0, amount=1_000_000):
    return fomo_tx(sig, user, pre=[bal(2, USDC, user, 100), bal(3, MINT, user, 0)],
                   post=[bal(2, USDC, user, 100 - usd), bal(3, MINT, user, amount)])


# ---------------------------------------------------------------- clasificación de transacciones
def test_parse_buy_with_usdc():
    t = parse_fomo_tx(buy_tx(usd=50))
    assert t and (t.side, t.mint, t.wallet, t.usd, t.token_amount) == ("buy", MINT, USER, 50, 1_000_000)


def test_parse_sell_to_usdc():
    tx = fomo_tx(pre=[bal(2, USDC, USER, 0), bal(3, MINT, USER, 500)], post=[bal(2, USDC, USER, 30), bal(3, MINT, USER, 0)])
    t = parse_fomo_tx(tx)
    assert t and t.side == "sell" and t.usd == 30


def test_parse_buy_with_sol_ignores_rent_dust():
    # paga 1.5 SOL, recibe el token; la devolución de rent de wSOL (0.002) no cuenta
    tx = fomo_tx(pre=[bal(3, MINT, USER, 0)], post=[bal(3, MINT, USER, 10)], lamports=(2_000_000_000, 500_000_000))
    t = parse_fomo_tx(tx)
    assert t and t.side == "buy" and t.sol == pytest.approx(1.5) and t.usd is None


@pytest.mark.parametrize("tx", [
    fomo_tx(pre=[bal(2, USDC, USER, 100)], post=[bal(2, USDC, USER, 50)]),             # depósito/transferencia
    fomo_tx(pre=[bal(3, MINT, USER, 0)], post=[bal(3, MINT, USER, 10)]),               # token recibido sin pagar
    buy_tx() | {"meta": buy_tx()["meta"] | {"err": {"InstructionError": [0, "x"]}}},  # tx fallida
    fomo_tx(cosigner=False, pre=[bal(2, USDC, USER, 100), bal(3, MINT, USER, 0)],
            post=[bal(2, USDC, USER, 50), bal(3, MINT, USER, 1)]) | {"transaction": {
                "signatures": ["x"], "message": {"accountKeys": [{"pubkey": "Other", "signer": True},
                                                                 {"pubkey": USER, "signer": True}]}}},  # no es FOMO
])
def test_parse_ignores_non_trades(tx):
    assert parse_fomo_tx(tx) is None


def test_aggregator_window_and_dedupe():
    agg = FlowAggregator(window_s=300)
    t0 = 1000.0
    for i, w in enumerate(["a", "b", "b", "c"]):
        assert agg.add(FomoTrade(f"s{i}", w, MINT, "buy", 1, 20, None, t0 + i))
    assert not agg.add(FomoTrade("s0", "a", MINT, "buy", 1, 20, None, t0))  # duplicado
    agg.add(FomoTrade("s9", "d", MINT, "sell", 1, 5, None, t0 + 5))
    st = agg.stats(MINT)
    assert (st.buys, st.sells, st.buyers, st.usd_buy) == (4, 1, 3, 80)
    assert agg.hot(3) == [MINT] and agg.hot(4) == []
    agg.prune(t0 + 400)
    assert agg.hot(1) == []


# ---------------------------------------------------------------- normalizador con formatos de fomo/Codex
def test_normalize_fomo_codex_shapes():
    src = JsonHttpTokenSource(name="fomo:bridge", url="", url_template=TPL)
    codex_like = {"success": True, "responseObject": {"tokens": [
        {"marketCap": "720000", "liquidity": "170000", "createdAt": 1790000000, "holders": 120,
         "token": {"address": MINT, "symbol": "TRUMP1", "name": "Trump Dollar", "networkId": 1399811149}},
        {"tokenId": "0xABC:8453", "symbol": "CENT", "name": "Cent"},
    ]}}
    a, b = src.parse_payload(codex_like)
    assert (a.chain, a.token_address, a.liquidity_usd, a.holder_count) == ("solana", MINT, 170000, 120)
    assert a.fomo_url == f"https://fomo.family/tokens/solana/{MINT}"
    assert (b.chain, b.token_address) == ("base", "0xABC")
    single = {"responseObject": {"address": MINT, "symbol": "TRUMP1", "networkId": 1399811149}}
    assert [t.symbol for t in src.parse_payload(single)] == ["TRUMP1"]


# ---------------------------------------------------------------- endpoint del userscript
def test_ingest_endpoint_requires_token_and_ingests(monkeypatch, tmp_path):
    s = Settings(demo_mode=True, db_path=str(tmp_path / "t.db"), ingest_token="secreto")
    from radar.api.server import create_app

    with TestClient(create_app(s)) as client:
        payload = {"url": "https://prod-api.fomo.family/proxy/trendingTokens",
                   "payload": {"responseObject": [{"address": MINT, "symbol": "TRUMP1", "name": "Trump Dollar",
                                                   "networkId": 1399811149, "liquidity": 9000}]}}
        assert client.post("/api/ingest/fomo", json=payload).status_code == 401
        r = client.post("/api/ingest/fomo", json=payload, headers={"x-radar-token": "secreto"})
        assert r.json() == {"accepted": 1}
        radar = client.app.state.radar
        assert f"solana:{MINT}" in radar.scanner.tokens
        assert radar.scanner.tokens[f"solana:{MINT}"].on_fomo


# ---------------------------------------------------------------- Vía D de punta a punta con un RPC falso
class _RPCHandler(BaseHTTPRequestHandler):
    txs: dict = {}

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        result = self.txs.get(body["params"][0])
        data = json.dumps({"jsonrpc": "2.0", "id": 1, "result": result}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


@pytest.mark.asyncio
async def test_flow_source_end_to_end_logs_transport(monkeypatch):
    import websockets

    sigs = [f"sig{i}" for i in range(3)]
    _RPCHandler.txs = {s: buy_tx(sig=s, user=f"wallet{i}", usd=25) for i, s in enumerate(sigs)}
    http = HTTPServer(("127.0.0.1", 0), _RPCHandler)
    threading.Thread(target=http.serve_forever, daemon=True).start()

    async def rpc_ws(ws):
        req = json.loads(await ws.recv())
        assert req["method"] == "logsSubscribe" and req["params"][0] == {"mentions": [CO_SIGNER]}
        await ws.send(json.dumps({"jsonrpc": "2.0", "result": 7, "id": 1}))
        for s in sigs:
            await ws.send(json.dumps({"jsonrpc": "2.0", "method": "logsNotification",
                                      "params": {"result": {"value": {"signature": s, "err": None, "logs": []}},
                                                 "subscription": 7}}))
        await ws.wait_closed()

    async def fake_tokens(self, chain, addresses):
        return [TokenInfo(name="Trump Dollar", symbol="TRUMP1", token_address=a, chain="solana",
                          liquidity_usd=80_000, created_at=utcnow(), source="dexscreener") for a in addresses]

    monkeypatch.setattr(DexScreenerClient, "tokens", fake_tokens)
    async with websockets.serve(rpc_ws, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        s = Settings(solana_rpc_wss=f"ws://127.0.0.1:{port}", solana_rpc_https=f"http://127.0.0.1:{http.server_port}",
                     fomo_flow_min_buyers=2, fomo_flow_max_rps=50)
        src = FomoFlowSource(s)
        stream = src.stream()
        tok = await asyncio.wait_for(anext(stream), timeout=10)
        await stream.aclose()
    http.shutdown()
    assert (tok.symbol, tok.on_fomo, tok.source) == ("TRUMP1", True, "fomo:flow")
    assert tok.fomo_buyers_5m >= 2 and tok.fomo_volume_usd_5m >= 50
    assert tok.fomo_url == f"https://fomo.family/tokens/solana/{MINT}"
