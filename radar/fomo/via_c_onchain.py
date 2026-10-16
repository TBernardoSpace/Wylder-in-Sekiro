"""Vía C — Fallback on-chain: DexScreener (perfiles/boosts recientes) + Pump.fun (mints recientes).

No depende de fomo.family; detecta los mismos tokens porque fomo lista tokens on-chain de Solana/Base.
Si también corre la Vía A/B, el scanner marca `on_fomo=True` en los tokens que aparecen en ambas,
y con REQUIRE_ON_FOMO=true solo esos pueden disparar alertas.
"""
from __future__ import annotations

import logging
from collections import defaultdict

import httpx

from ..config import Settings
from ..models import TokenInfo
from ..onchain.dexscreener import DexScreenerClient
from ..utils import to_datetime, to_float
from .base import TokenSource

log = logging.getLogger(__name__)


class DexScreenerNewTokensSource(TokenSource):
    name = "onchain:dexscreener"
    on_fomo = False

    def __init__(self, s: Settings) -> None:
        self.s = s
        self._known: set[str] = set()

    async def poll(self, client: httpx.AsyncClient) -> list[TokenInfo]:
        dex = DexScreenerClient(client, self.s.fomo_token_url_template)
        entries = await dex.latest_profiles()
        try:
            entries += await dex.latest_boosts()
        except httpx.HTTPError as exc:
            log.debug("boosts no disponible: %s", exc)
        by_chain: dict[str, list[str]] = defaultdict(list)
        for e in entries:
            chain, addr = e.get("chainId"), e.get("tokenAddress")
            if chain in self.s.chains and addr and addr not in by_chain[chain]:
                by_chain[chain].append(addr)
        out: list[TokenInfo] = []
        for chain, addrs in by_chain.items():
            out.extend(await dex.tokens(chain, addrs))
        for t in out:
            t.source = self.name
        return out


class PumpFunSource(TokenSource):
    """Endpoint no oficial del frontend de pump.fun (cambia de versión a menudo: PUMPFUN_URL)."""

    name = "onchain:pumpfun"
    on_fomo = False

    def __init__(self, s: Settings) -> None:
        self.s = s

    async def poll(self, client: httpx.AsyncClient) -> list[TokenInfo]:
        if "solana" not in self.s.chains:
            return []
        r = await client.get(self.s.pumpfun_url, headers={"Accept": "application/json",
                                                           "Origin": "https://pump.fun"})
        r.raise_for_status()
        data = r.json()
        coins = data if isinstance(data, list) else data.get("coins", [])
        out = []
        for c in coins:
            mint, symbol = c.get("mint"), c.get("symbol")
            if not mint or not symbol:
                continue
            out.append(TokenInfo(
                name=c.get("name") or symbol,
                symbol=symbol,
                token_address=mint,
                chain="solana",
                market_cap=to_float(c.get("usd_market_cap")),
                created_at=to_datetime(c.get("created_timestamp")),
                fomo_url=self.s.fomo_token_url_template.format(chain="solana", address=mint),
                dexscreener_url=f"https://dexscreener.com/solana/{mint}",
                source=self.name,
            ))
        return out


def build_onchain_sources(s: Settings) -> list[TokenSource]:
    out: list[TokenSource] = []
    if "dexscreener" in s.fomo_c_sources:
        out.append(DexScreenerNewTokensSource(s))
    if "pumpfun" in s.fomo_c_sources:
        out.append(PumpFunSource(s))
    return out
