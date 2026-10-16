"""Vía D — Flujo on-chain de la app FOMO en Solana (sin API de fomo, sin Cloudflare).

Hallazgo clave: la app FOMO firma y paga TODAS sus transacciones en Solana con una única cuenta
co-firmante, `AgmLJBMDCqWynYnQiPCuj9ewsNNsBJXyzoUhD9LJzN51` (documentado por Chainstack Labs en
github.com/chainstacklabs/fomo-solana-rh-listeners, verificado on-chain 2026-09-09). Cada wallet de
usuario es un keypair normal, así que esa firma es la única marca, y alcanza: suscribiéndonos a
ella vemos en vivo qué tokens están comprando los usuarios de FOMO, segundos después de cada compra.

Transportes:
  logs   -> `logsSubscribe {mentions:[co-signer]}` + `getTransaction` por firma. Funciona en
            cualquier RPC (Helius, QuickNode, Chainstack, Triton...). Cuesta 1 request por trade.
  blocks -> `blockSubscribe {mentionsAccountOrProgram}` con transacciones completas: sin requests
            extra, pero solo en RPCs que lo habilitan (p. ej. Chainstack, QuickNode).

Las compras que cruzan de cadena (pagar en Solana y recibir en Base/Robinhood) no revelan el token
en Solana; esta vía solo ve con certeza las compras de tokens de Solana, que son el caso pump.fun.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any, AsyncIterator

import httpx

from ..config import Settings
from ..models import TokenInfo
from ..onchain.dexscreener import DexScreenerClient
from ..utils import utcnow
from .base import TokenSource

log = logging.getLogger(__name__)

CO_SIGNER = "AgmLJBMDCqWynYnQiPCuj9ewsNNsBJXyzoUhD9LJzN51"
WSOL = "So11111111111111111111111111111111111111112"
STABLES = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "USDC",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": "USDT",
    "2u1tszSeqZ3qBWF3uNGPFc8TzMk2tdiwknnRMWGWjGWH": "USDG",
}
QUOTES = set(STABLES) | {WSOL, "SOL"}
SOL_DUST = 0.005  # rent de cuentas de token que se devuelve al cerrar wSOL
MAX_FRAME_BYTES = 32 * 1024 * 1024


@dataclass
class FomoTrade:
    signature: str
    wallet: str
    mint: str
    side: str  # buy | sell
    token_amount: float
    usd: float | None  # solo si el otro lado fue un stable (el saldo de FOMO es USDC)
    sol: float | None
    ts: float


def parse_fomo_tx(entry: dict[str, Any], now: float | None = None) -> FomoTrade | None:
    """Clasifica una transacción `jsonParsed` (de getTransaction o de un bloque).

    Devuelve un trade solo si el usuario de FOMO dio un "quote" (USDC/USDT/SOL) y recibió
    exactamente un token (compra), o al revés (venta). Transferencias, depósitos cross-chain y
    swaps token->token se ignoran.
    """
    meta = entry.get("meta") or {}
    if meta.get("err"):
        return None
    tx = entry.get("transaction") or {}
    msg = tx.get("message") or {}
    keys_raw = msg.get("accountKeys") or []
    keys = [k["pubkey"] if isinstance(k, dict) else k for k in keys_raw]
    signers = [k["pubkey"] for k in keys_raw if isinstance(k, dict) and k.get("signer")]
    if CO_SIGNER not in signers and CO_SIGNER not in keys[:2]:
        return None
    user = next((s for s in signers if s != CO_SIGNER), None)
    if not user:
        return None

    deltas: dict[str, float] = defaultdict(float)
    for sign, balances in ((-1, meta.get("preTokenBalances") or []), (1, meta.get("postTokenBalances") or [])):
        for b in balances:
            if b.get("owner") != user:
                continue
            amt = (b.get("uiTokenAmount") or {}).get("uiAmountString")
            deltas[b["mint"]] += sign * float(amt or 0)
    if user in keys:
        i = keys.index(user)
        pre, post = meta.get("preBalances") or [], meta.get("postBalances") or []
        if i < len(pre) and i < len(post):
            deltas["SOL"] += (post[i] - pre[i]) / 1e9

    moved = {m: d for m, d in deltas.items() if abs(d) > 1e-12}
    if len(moved) > 1:  # la devolución de rent de wSOL no es parte del trade
        moved = {m: d for m, d in moved.items() if m not in ("SOL", WSOL) or abs(d) >= SOL_DUST}
    tokens = {m: d for m, d in moved.items() if m not in QUOTES}
    quotes = {m: d for m, d in moved.items() if m in QUOTES}
    if len(tokens) != 1 or not quotes:
        return None
    mint, amount = next(iter(tokens.items()))
    side = "buy" if amount > 0 else "sell"
    # El quote tiene que moverse en sentido contrario al token
    opposite = {m: d for m, d in quotes.items() if (d < 0) == (side == "buy")}
    if not opposite:
        return None
    usd = sum(abs(d) for m, d in opposite.items() if m in STABLES) or None
    sol = sum(abs(d) for m, d in opposite.items() if m in ("SOL", WSOL)) or None
    sig = (tx.get("signatures") or [""])[0]
    ts = float(entry.get("blockTime") or now or time.time())
    return FomoTrade(sig, user, mint, side, abs(amount), usd, sol, ts)


@dataclass
class FlowStats:
    buys: int
    sells: int
    buyers: int
    usd_buy: float


class FlowAggregator:
    """Ventana deslizante por mint: compras, ventas, compradores únicos y USD comprado."""

    def __init__(self, window_s: float) -> None:
        self.window = window_s
        self._ev: dict[str, deque[FomoTrade]] = defaultdict(deque)
        self._seen: set[str] = set()
        self._seen_q: deque[str] = deque()

    def add(self, t: FomoTrade) -> bool:
        if t.signature and t.signature in self._seen:
            return False  # el mismo trade puede llegar dos veces (reintentos / dos transportes)
        if t.signature:
            self._seen.add(t.signature)
            self._seen_q.append(t.signature)
            if len(self._seen_q) > 50_000:
                self._seen.discard(self._seen_q.popleft())
        self._ev[t.mint].append(t)
        return True

    def prune(self, now: float) -> None:
        cutoff = now - self.window
        for mint in list(self._ev):
            dq = self._ev[mint]
            while dq and dq[0].ts < cutoff:
                dq.popleft()
            if not dq:
                del self._ev[mint]

    def stats(self, mint: str) -> FlowStats:
        dq = self._ev.get(mint, deque())
        buys = [t for t in dq if t.side == "buy"]
        return FlowStats(len(buys), len(dq) - len(buys), len({t.wallet for t in buys}),
                         round(sum(t.usd or 0 for t in buys), 2))

    def hot(self, min_buyers: int) -> list[str]:
        return [m for m in self._ev if self.stats(m).buyers >= min_buyers]


class FomoFlowSource(TokenSource):
    name = "fomo:flow"
    on_fomo = True

    def __init__(self, s: Settings) -> None:
        if not s.solana_rpc_wss:
            raise ValueError("FOMO_MODES incluye 'd' pero SOLANA_RPC_WSS está vacío")
        if s.fomo_flow_transport == "logs" and not s.solana_rpc_https:
            raise ValueError("FOMO_FLOW_TRANSPORT=logs necesita SOLANA_RPC_HTTPS para getTransaction")
        self.s = s
        self.agg = FlowAggregator(s.fomo_flow_window_s)
        self._sigs: asyncio.Queue[str] = asyncio.Queue(maxsize=5000)
        self._meta: dict[str, TokenInfo | None] = {}  # mint -> metadatos (None = viejo/descartado)
        self._meta_retry: dict[str, float] = {}
        self._last_emit: dict[str, tuple] = {}
        self.dropped = 0
        self.trades = 0

    async def poll(self, client: httpx.AsyncClient) -> list[TokenInfo]:
        return []  # solo push

    def stream(self) -> AsyncIterator[TokenInfo]:
        return self._run()

    async def _run(self) -> AsyncIterator[TokenInfo]:
        async with httpx.AsyncClient(timeout=10) as client:
            tasks = [asyncio.create_task(self._listen())]
            if self.s.fomo_flow_transport == "logs":
                tasks += [asyncio.create_task(self._fetch_worker(client)) for _ in range(4)]
            dex = DexScreenerClient(client, self.s.fomo_token_url_template)
            try:
                while True:
                    await asyncio.sleep(3)
                    for tok in await self._flush(dex):
                        yield tok
            finally:
                for t in tasks:
                    t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    # ------------------------------------------------------------------ transporte
    async def _listen(self) -> None:
        import websockets

        if self.s.fomo_flow_transport == "blocks":
            sub = {"jsonrpc": "2.0", "id": 1, "method": "blockSubscribe", "params": [
                {"mentionsAccountOrProgram": CO_SIGNER},
                {"commitment": "confirmed", "encoding": "jsonParsed", "showRewards": False,
                 "transactionDetails": "full", "maxSupportedTransactionVersion": 0}]}
        else:
            sub = {"jsonrpc": "2.0", "id": 1, "method": "logsSubscribe",
                   "params": [{"mentions": [CO_SIGNER]}, {"commitment": "confirmed"}]}
        backoff = 1.0
        while True:
            try:
                async with websockets.connect(self.s.solana_rpc_wss, max_size=MAX_FRAME_BYTES,
                                              ping_interval=20) as ws:
                    await ws.send(json.dumps(sub))
                    first = json.loads(await ws.recv())
                    if first.get("error"):
                        raise RuntimeError(f"el RPC rechazó la suscripción: {first['error']}")
                    log.info("Vía D: escuchando trades de FOMO (%s)", self.s.fomo_flow_transport)
                    backoff = 1.0
                    async for raw in ws:
                        self._on_message(json.loads(raw))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("Vía D: WS Solana caído (%s); reintento en %.0fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    def _on_message(self, msg: dict) -> None:
        value = ((msg.get("params") or {}).get("result") or {}).get("value") or {}
        if "signature" in value:  # logsNotification
            if value.get("err"):
                return
            try:
                self._sigs.put_nowait(value["signature"])
            except asyncio.QueueFull:
                self.dropped += 1
        elif "block" in value:  # blockNotification
            block = value.get("block") or {}
            for entry in block.get("transactions") or []:
                entry.setdefault("blockTime", block.get("blockTime"))
                self._record(parse_fomo_tx(entry))

    async def _fetch_worker(self, client: httpx.AsyncClient) -> None:
        interval = 4 / max(self.s.fomo_flow_max_rps, 0.1)  # 4 workers comparten el presupuesto
        while True:
            sig = await self._sigs.get()
            for attempt in range(3):
                started = time.monotonic()
                try:
                    r = await client.post(self.s.solana_rpc_https, json={
                        "jsonrpc": "2.0", "id": 1, "method": "getTransaction",
                        "params": [sig, {"encoding": "jsonParsed", "commitment": "confirmed",
                                         "maxSupportedTransactionVersion": 0}]})
                    if r.status_code == 429:
                        await asyncio.sleep(1 + attempt)
                        continue
                    result = r.json().get("result")
                except (httpx.HTTPError, ValueError) as exc:
                    log.debug("getTransaction %s falló: %s", sig[:8], exc)
                    result = None
                if result:
                    self._record(parse_fomo_tx(result))
                    break
                await asyncio.sleep(0.8)  # aún no indexada en el nodo: reintenta
                await asyncio.sleep(max(0.0, interval - (time.monotonic() - started)))
            await asyncio.sleep(interval)

    def _record(self, trade: FomoTrade | None) -> None:
        if trade and self.agg.add(trade):
            self.trades += 1
            log.debug("FOMO %s %s %.4g (%s) por %s", trade.side, trade.mint[:8], trade.token_amount,
                      f"${trade.usd:.0f}" if trade.usd else f"{trade.sol or 0:.2f} SOL", trade.wallet[:6])

    # ------------------------------------------------------------------ emisión
    async def _flush(self, dex: DexScreenerClient) -> list[TokenInfo]:
        now = time.time()
        self.agg.prune(now)
        hot = self.agg.hot(self.s.fomo_flow_min_buyers)
        unknown = [m for m in hot if m not in self._meta and self._meta_retry.get(m, 0) <= now]
        if unknown:
            await self._resolve(dex, unknown, now)
        out = []
        for mint in hot:
            base = self._meta.get(mint)
            if base is None:
                continue
            st = self.agg.stats(mint)
            sig = (st.buys, st.sells, st.buyers, st.usd_buy)
            if self._last_emit.get(mint) == sig:
                continue
            update: dict[str, Any] = {
                "fomo_buys_5m": st.buys, "fomo_sells_5m": st.sells, "fomo_buyers_5m": st.buyers,
                "fomo_volume_usd_5m": st.usd_buy, "source": self.name, "on_fomo": True,
                "last_update": utcnow()}
            if mint in self._last_emit:
                # Las métricas de mercado cacheadas ya son viejas: solo viaja el flujo FOMO para no
                # pisar los datos frescos que el motor obtiene de DexScreener.
                update |= dict.fromkeys(("market_cap", "liquidity_usd", "volume_5m", "holder_count",
                                         "buys_5m", "sells_5m", "price_usd"))
            self._last_emit[mint] = sig
            out.append(base.model_copy(update=update))
        return out

    async def _resolve(self, dex: DexScreenerClient, mints: list[str], now: float) -> None:
        """Nombre/ticker/métricas vía DexScreener. Tokens viejos (> TOKEN_MAX_AGE) se ignoran para
        siempre; los aún no indexados se reintentan en 15 s."""
        try:
            found = {t.token_address: t for t in await dex.tokens("solana", mints)}
        except httpx.HTTPError as exc:
            log.info("Vía D: DexScreener no respondió (%s)", exc)
            for m in mints:
                self._meta_retry[m] = now + 15
            return
        max_age = self.s.token_max_age_minutes * 60
        for m in mints:
            t = found.get(m)
            if t is None:
                self._meta_retry[m] = now + 15
            elif t.created_at and (utcnow() - t.created_at).total_seconds() > max_age:
                self._meta[m] = None
            else:
                t.fomo_url = self.s.fomo_token_url_template.format(chain="solana", address=m)
                self._meta[m] = t
