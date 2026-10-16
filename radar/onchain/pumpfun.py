"""Pump.fun: decodificación de eventos on-chain y metadata de tokens.

Fuente de verdad: IDL oficial publicado por pump.fun en github.com/pump-fun/pump-public-docs
(`idl/pump.json`, programa 6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P). El programa emite sus
eventos Anchor en los logs como `Program data: <base64>`; los 8 primeros bytes son el discriminador.

  CreateEvent  [27,114,169,77,222,235,99,118]  name, symbol, uri, mint, bonding_curve, user, creator,
                                                timestamp, virtual_token_reserves, virtual_sol_reserves,
                                                real_token_reserves, token_total_supply, ...
  TradeEvent   [189,219,127,211,78,230,97,238] mint, sol_amount, token_amount, is_buy, user, timestamp,
                                                virtual_sol_reserves, virtual_token_reserves, ...
  CompleteEvent [95,114,97,156,212,46,152,8]   user, mint, bonding_curve, timestamp (graduación)

Los campos se leen en orden y los que faltan al final se toleran (versiones viejas del evento).
"""
from __future__ import annotations

import asyncio
import base64
import logging
import re
import struct
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger(__name__)

PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
CREATE_DISC = bytes([27, 114, 169, 77, 222, 235, 99, 118])
TRADE_DISC = bytes([189, 219, 127, 211, 78, 230, 97, 238])
COMPLETE_DISC = bytes([95, 114, 97, 156, 212, 46, 152, 8])
TOKEN_DECIMALS = 6
LAMPORTS = 1e9

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58encode(data: bytes) -> str:
    n = int.from_bytes(data, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = _B58[r] + out
    pad = len(data) - len(data.lstrip(b"\0"))
    return "1" * pad + out


class _Reader:
    def __init__(self, data: bytes) -> None:
        self.d, self.i = data, 0

    def take(self, n: int) -> bytes:
        if self.i + n > len(self.d):
            raise EOFError
        chunk = self.d[self.i:self.i + n]
        self.i += n
        return chunk

    def string(self) -> str:
        (n,) = struct.unpack("<I", self.take(4))
        if n > 2048:
            raise ValueError("string demasiado larga")
        return self.take(n).decode("utf-8", "replace")

    def pubkey(self) -> str:
        return b58encode(self.take(32))

    def u64(self) -> int:
        return struct.unpack("<Q", self.take(8))[0]

    def i64(self) -> int:
        return struct.unpack("<q", self.take(8))[0]

    def boolean(self) -> bool:
        return self.take(1) != b"\0"


@dataclass
class PumpCreate:
    name: str
    symbol: str
    uri: str
    mint: str
    bonding_curve: str
    user: str
    creator: str | None = None
    timestamp: int | None = None
    virtual_token_reserves: int | None = None
    virtual_sol_reserves: int | None = None
    real_token_reserves: int | None = None
    token_total_supply: int | None = None


@dataclass
class PumpTrade:
    mint: str
    sol_amount: int
    token_amount: int
    is_buy: bool
    user: str
    timestamp: int
    virtual_sol_reserves: int | None = None
    virtual_token_reserves: int | None = None
    real_sol_reserves: int | None = None
    real_token_reserves: int | None = None


@dataclass
class PumpComplete:
    user: str
    mint: str
    bonding_curve: str
    timestamp: int


def decode_event(data: bytes) -> PumpCreate | PumpTrade | PumpComplete | None:
    disc, r = data[:8], _Reader(data[8:])
    try:
        if disc == CREATE_DISC:
            ev = PumpCreate(r.string(), r.string(), r.string(), r.pubkey(), r.pubkey(), r.pubkey())
            try:
                ev.creator, ev.timestamp = r.pubkey(), r.i64()
                ev.virtual_token_reserves, ev.virtual_sol_reserves = r.u64(), r.u64()
                ev.real_token_reserves, ev.token_total_supply = r.u64(), r.u64()
            except EOFError:
                pass  # versión antigua del evento, con menos campos
            return ev
        if disc == TRADE_DISC:
            ev = PumpTrade(r.pubkey(), r.u64(), r.u64(), r.boolean(), r.pubkey(), r.i64())
            try:
                ev.virtual_sol_reserves, ev.virtual_token_reserves = r.u64(), r.u64()
                ev.real_sol_reserves, ev.real_token_reserves = r.u64(), r.u64()
            except EOFError:
                pass
            return ev
        if disc == COMPLETE_DISC:
            return PumpComplete(r.pubkey(), r.pubkey(), r.pubkey(), r.i64())
    except (EOFError, ValueError, struct.error):
        return None
    return None


def events_from_logs(logs: list[str]) -> list[PumpCreate | PumpTrade | PumpComplete]:
    out = []
    for line in logs or []:
        if not line.startswith("Program data: "):
            continue
        try:
            raw = base64.b64decode(line[len("Program data: "):])
        except ValueError:
            continue
        ev = decode_event(raw)
        if ev:
            out.append(ev)
    return out


def price_sol(virtual_sol: int | None, virtual_token: int | None) -> float | None:
    """Precio en SOL por token según la curva (SOL 9 decimales, token 6)."""
    if not virtual_sol or not virtual_token:
        return None
    return (virtual_sol / LAMPORTS) / (virtual_token / 10**TOKEN_DECIMALS)


# ---------------------------------------------------------------------------- metadata (IPFS)
@dataclass
class PumpMetadata:
    name: str | None = None
    symbol: str | None = None
    description: str = ""
    twitter: str | None = None
    telegram: str | None = None
    website: str | None = None
    image: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


_IPFS_RE = re.compile(r"(?:ipfs://|/ipfs/)([A-Za-z0-9]{40,})(/.*)?$")


def gateway_url(uri: str, gateway: str) -> str:
    m = _IPFS_RE.search(uri or "")
    if m and gateway:
        return gateway.rstrip("/") + "/" + m.group(1) + (m.group(2) or "")
    return uri


class MetadataFetcher:
    """Descarga el JSON de metadata (descripción, twitter, web...) con caché y concurrencia acotada."""

    def __init__(self, client: httpx.AsyncClient, gateway: str, concurrency: int = 8) -> None:
        self.client, self.gateway = client, gateway
        self._sem = asyncio.Semaphore(concurrency)
        self._cache: dict[str, PumpMetadata | None] = {}

    async def fetch(self, uri: str) -> PumpMetadata | None:
        if not uri or not uri.startswith(("http", "ipfs")):
            return None
        if uri in self._cache:
            return self._cache[uri]
        async with self._sem:
            meta = None
            for url in dict.fromkeys([gateway_url(uri, self.gateway), uri]):  # gateway propio y original
                if not url.startswith("http"):
                    continue
                try:
                    r = await self.client.get(url, timeout=5, follow_redirects=True)
                    if r.status_code == 200:
                        meta = parse_metadata(r.json())
                        break
                except (httpx.HTTPError, ValueError):
                    continue
        if len(self._cache) > 20_000:
            self._cache.clear()
        self._cache[uri] = meta
        return meta


def parse_metadata(data: Any) -> PumpMetadata:
    if not isinstance(data, dict):
        return PumpMetadata()
    ext = data.get("extensions") if isinstance(data.get("extensions"), dict) else {}

    def pick(*keys: str) -> str | None:
        for k in keys:
            v = data.get(k) or ext.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()[:300]
        return None

    return PumpMetadata(
        name=pick("name"),
        symbol=pick("symbol"),
        description=(pick("description") or "")[:600],
        twitter=pick("twitter", "x"),
        telegram=pick("telegram"),
        website=pick("website", "external_url"),
        image=pick("image"),
    )
