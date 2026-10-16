"""Vía C+ — Stream en tiempo real de pump.fun (fomo lista estos tokens: mints terminados en `pump`).

Dos transportes (PUMPFUN_STREAM):
  pumpportal -> wss://pumpportal.fun/api/data `subscribeNewToken` (gratis): cada lanzamiento al
                instante (mint, name, symbol, uri, creador, market cap inicial en SOL).
  rpc        -> `logsSubscribe` al programa de pump.fun en TU RPC de Solana (SOLANA_RPC_WSS) y
                decodificación de los eventos oficiales del IDL: lanzamientos Y cada compra/venta en
                la curva, de donde salen market cap, volumen 5m, compras/ventas y compradores únicos
                en vivo, antes de que DexScreener indexe el token. Mucho tráfico: usa un RPC de pago.

En ambos casos se descarga la metadata del token (IPFS): descripción, twitter, telegram y web.
Los creadores suelen poner ahí el link al tweet o a la noticia, lo que mejora mucho el cruce.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncIterator

import httpx

from ..config import Settings
from ..models import TokenInfo
from ..onchain.dexscreener import DexScreenerClient
from ..onchain.pumpfun import (
    LAMPORTS, PUMP_PROGRAM, TOKEN_DECIMALS, MetadataFetcher, PumpComplete, PumpCreate, PumpTrade,
    events_from_logs, parse_metadata, price_sol,
)
from ..utils import to_datetime, to_float
from .base import TokenSource

log = logging.getLogger(__name__)
WSOL = "So11111111111111111111111111111111111111112"
DEFAULT_SUPPLY = 1_000_000_000  # tokens (pump.fun crea 1.000M con 6 decimales)


@dataclass
class _Curve:
    supply: float = DEFAULT_SUPPLY
    price_sol: float | None = None
    real_sol: float | None = None
    trades: deque = field(default_factory=deque)  # (ts, sol, is_buy, user)
    dirty: bool = False
    graduated: bool = False


class PumpFunStreamSource(TokenSource):
    name = "pumpfun:stream"
    on_fomo = False

    def __init__(self, s: Settings) -> None:
        if s.pumpfun_stream == "rpc" and not s.solana_rpc_wss:
            raise ValueError("PUMPFUN_STREAM=rpc necesita SOLANA_RPC_WSS")
        self.s = s
        self.curves: dict[str, _Curve] = {}
        self.sol_usd: float | None = s.sol_price_usd or None
        self._out: asyncio.Queue[TokenInfo] = asyncio.Queue(maxsize=10_000)
        self._tasks: set[asyncio.Task] = set()
        self.created = 0
        self.trades = 0

    async def poll(self, client: httpx.AsyncClient) -> list[TokenInfo]:
        return []

    def stream(self) -> AsyncIterator[TokenInfo] | None:
        return None if self.s.pumpfun_stream == "off" else self._run()

    async def _run(self) -> AsyncIterator[TokenInfo]:
        async with httpx.AsyncClient(timeout=8) as client:
            self._meta = MetadataFetcher(client, self.s.ipfs_gateway)
            self._client = client
            jobs = [self._listen(), self._sol_price_loop(DexScreenerClient(client, ""))]
            if self.s.pumpfun_stream == "rpc":
                jobs.append(self._flush_loop())
            tasks = [asyncio.create_task(j) for j in jobs]
            try:
                while True:
                    yield await self._out.get()
            finally:
                for t in tasks + list(self._tasks):
                    t.cancel()
                await asyncio.gather(*tasks, *self._tasks, return_exceptions=True)

    # ------------------------------------------------------------------ transporte
    async def _listen(self) -> None:
        import websockets

        if self.s.pumpfun_stream == "rpc":
            url = self.s.solana_rpc_wss
            subs = [{"jsonrpc": "2.0", "id": 1, "method": "logsSubscribe",
                     "params": [{"mentions": [PUMP_PROGRAM]}, {"commitment": "confirmed"}]}]
        else:
            url = self.s.pumpportal_wss
            subs = [{"method": "subscribeNewToken"}]
        backoff = 1.0
        while True:
            try:
                async with websockets.connect(url, max_size=2**24, ping_interval=20) as ws:
                    for sub in subs:
                        await ws.send(json.dumps(sub))
                    log.info("pump.fun: stream %s conectado", self.s.pumpfun_stream)
                    backoff = 1.0
                    async for raw in ws:
                        try:
                            self.on_message(json.loads(raw))
                        except (ValueError, KeyError, TypeError) as exc:
                            log.debug("pump.fun: mensaje no válido: %s", exc)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("pump.fun: stream caído (%s); reintento en %.0fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    def on_message(self, msg: dict[str, Any]) -> None:
        if "params" in msg:  # logsNotification del RPC
            value = (msg["params"].get("result") or {}).get("value") or {}
            if value.get("err"):
                return
            for ev in events_from_logs(value.get("logs") or []):
                self.on_event(ev)
        elif msg.get("txType") == "create" and msg.get("mint"):  # PumpPortal
            self._on_pumpportal_create(msg)

    # ------------------------------------------------------------------ eventos
    def on_event(self, ev: PumpCreate | PumpTrade | PumpComplete) -> None:
        if isinstance(ev, PumpCreate):
            curve = _Curve()
            if ev.token_total_supply:
                curve.supply = ev.token_total_supply / 10**TOKEN_DECIMALS
            curve.price_sol = price_sol(ev.virtual_sol_reserves, ev.virtual_token_reserves)
            self.curves[ev.mint] = curve
            self._emit_new(ev.mint, ev.name, ev.symbol, ev.uri, ev.user,
                           to_datetime(ev.timestamp) if ev.timestamp else None,
                           curve.price_sol * curve.supply if curve.price_sol else None)
        elif isinstance(ev, PumpTrade):
            curve = self.curves.get(ev.mint)
            if curve is None:
                return  # solo seguimos tokens lanzados desde que arrancó el radar
            self.trades += 1
            curve.trades.append((float(ev.timestamp or time.time()), ev.sol_amount / LAMPORTS, ev.is_buy, ev.user))
            curve.price_sol = price_sol(ev.virtual_sol_reserves, ev.virtual_token_reserves) or curve.price_sol
            if ev.real_sol_reserves is not None:
                curve.real_sol = ev.real_sol_reserves / LAMPORTS
            curve.dirty = True
        elif isinstance(ev, PumpComplete) and ev.mint in self.curves:
            self.curves[ev.mint].graduated = True
            log.info("pump.fun: %s completó la curva (migra a PumpSwap)", ev.mint)

    def _on_pumpportal_create(self, m: dict[str, Any]) -> None:
        mc_sol = to_float(m.get("marketCapSol"))
        symbol = m.get("symbol") or ""
        self._emit_new(m["mint"], m.get("name") or symbol, symbol, m.get("uri") or "",
                       m.get("traderPublicKey"), None, mc_sol)

    def _emit_new(self, mint: str, name: str, symbol: str, uri: str, creator: str | None,
                  created_at: datetime | None, mc_sol: float | None) -> None:
        self.created += 1
        task = asyncio.create_task(self._emit_with_metadata(mint, name, symbol, uri, creator, created_at, mc_sol))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _emit_with_metadata(self, mint: str, name: str, symbol: str, uri: str, creator: str | None,
                                  created_at: datetime | None, mc_sol: float | None) -> None:
        meta = None
        if not symbol.strip():  # el mensaje no trajo ticker: se toma de la metadata antes de publicar
            meta = await self._meta.fetch(uri) if uri else None
            if meta is None and self.s.pumpfun_coin_url:
                meta = await self._coin_api(mint)
            name, symbol = (meta.name or name) if meta else name, (meta.symbol or "") if meta else ""
            if not symbol.strip():
                return
        tok = TokenInfo(
            name=name.strip() or symbol or mint[:6], symbol=(symbol.strip() or "?")[:20], token_address=mint,
            chain="solana", created_at=created_at or datetime.now(timezone.utc),
            market_cap=mc_sol * self.sol_usd if mc_sol and self.sol_usd else None,
            creator=creator, fomo_url=self.s.fomo_token_url_template.format(chain="solana", address=mint),
            dexscreener_url=f"https://dexscreener.com/solana/{mint}", source=self.name)
        # Primero se publica el token (velocidad); la metadata llega como actualización.
        self._push(tok)
        if meta is None:
            meta = await self._meta.fetch(uri) if uri else None
        if meta is None and self.s.pumpfun_coin_url:
            meta = await self._coin_api(mint)
        if meta and (meta.description or meta.twitter or meta.website):
            self._push(tok.model_copy(update={
                "description": meta.description or None, "twitter": meta.twitter, "telegram": meta.telegram,
                "website": meta.website, "image_url": meta.image, "market_cap": None}))

    async def _coin_api(self, mint: str):
        try:
            r = await self._client.get(self.s.pumpfun_coin_url.format(mint=mint), timeout=4,
                                       headers={"Accept": "application/json", "Origin": "https://pump.fun"})
            return parse_metadata(r.json()) if r.status_code == 200 else None
        except (httpx.HTTPError, ValueError):
            return None

    def _push(self, tok: TokenInfo) -> None:
        try:
            self._out.put_nowait(tok)
        except asyncio.QueueFull:
            log.warning("pump.fun: cola llena, token %s descartado", tok.symbol)

    # ------------------------------------------------------------------ métricas de la curva (rpc)
    def snapshot(self, mint: str, now: float) -> TokenInfo | None:
        curve = self.curves.get(mint)
        if not curve:
            return None
        while curve.trades and curve.trades[0][0] < now - 300:
            curve.trades.popleft()
        buys = [t for t in curve.trades if t[2]]
        vol_sol = sum(t[1] for t in curve.trades)
        usd = self.sol_usd
        return TokenInfo(
            name="?", symbol="?", token_address=mint, chain="solana", source=self.name,
            buys_5m=len(buys), sells_5m=len(curve.trades) - len(buys),
            volume_5m=round(vol_sol * usd, 2) if usd else None,
            price_usd=curve.price_sol * usd if usd and curve.price_sol else None,
            market_cap=round(curve.price_sol * curve.supply * usd, 2) if usd and curve.price_sol else None,
            # SOL real en la curva x2 (ambos lados), misma convención que la liquidez de un pool AMM
            liquidity_usd=round(curve.real_sol * usd * 2, 2) if usd and curve.real_sol is not None else None,
        )

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(3)
            now = time.time()
            for mint, curve in list(self.curves.items()):
                if curve.dirty:
                    curve.dirty = False
                    snap = self.snapshot(mint, now)
                    if snap:
                        self._push(snap)
            if len(self.curves) > 20_000:  # ~1 día de lanzamientos: se descartan los más viejos
                for mint in list(self.curves)[: len(self.curves) - 15_000]:
                    del self.curves[mint]

    async def _sol_price_loop(self, dex: DexScreenerClient) -> None:
        if self.s.sol_price_usd:
            return
        while True:
            try:
                pairs = await dex.tokens("solana", [WSOL])
                if pairs and pairs[0].price_usd:
                    self.sol_usd = pairs[0].price_usd
            except httpx.HTTPError as exc:
                log.info("pump.fun: no se pudo obtener el precio de SOL (%s)", exc)
            await asyncio.sleep(60)
