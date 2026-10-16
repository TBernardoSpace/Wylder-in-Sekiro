"""Cliente DexScreener (API pública, sin clave). Docs: https://docs.dexscreener.com/api/reference

Límites aprox.: token-profiles/boosts 60 req/min; tokens/pairs 300 req/min.
"""
from __future__ import annotations

import logging

import httpx

from ..models import TokenInfo
from ..utils import to_datetime, to_float, to_int

log = logging.getLogger(__name__)

BASE = "https://api.dexscreener.com"


class DexScreenerClient:
    def __init__(self, client: httpx.AsyncClient, fomo_url_template: str) -> None:
        self.client = client
        self.fomo_tpl = fomo_url_template

    async def latest_profiles(self) -> list[dict]:
        """Tokens que acaban de crear perfil (suele ser señal de lanzamiento serio)."""
        r = await self.client.get(f"{BASE}/token-profiles/latest/v1")
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else []

    async def latest_boosts(self) -> list[dict]:
        r = await self.client.get(f"{BASE}/token-boosts/latest/v1")
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else []

    async def tokens(self, chain: str, addresses: list[str]) -> list[TokenInfo]:
        """Métricas de hasta 30 tokens por llamada; elige el par con más liquidez de cada token."""
        out: list[TokenInfo] = []
        for i in range(0, len(addresses), 30):
            chunk = addresses[i:i + 30]
            r = await self.client.get(f"{BASE}/tokens/v1/{chain}/{','.join(chunk)}")
            r.raise_for_status()
            pairs = r.json() or []
            best: dict[str, dict] = {}
            for p in pairs:
                addr = (p.get("baseToken") or {}).get("address")
                if not addr:
                    continue
                k = addr.lower() if addr.startswith("0x") else addr
                liq = to_float((p.get("liquidity") or {}).get("usd")) or 0
                if k not in best or liq > (to_float((best[k].get("liquidity") or {}).get("usd")) or 0):
                    best[k] = p
            out.extend(self.pair_to_token(p) for p in best.values())
        return out

    def pair_to_token(self, p: dict) -> TokenInfo:
        base = p.get("baseToken") or {}
        chain = p.get("chainId", "solana")
        addr = base.get("address", "")
        txns_m5 = (p.get("txns") or {}).get("m5") or {}
        return TokenInfo(
            name=base.get("name") or "?",
            symbol=base.get("symbol") or "?",
            token_address=addr,
            chain=chain,
            market_cap=to_float(p.get("marketCap")) or to_float(p.get("fdv")),
            liquidity_usd=to_float((p.get("liquidity") or {}).get("usd")),
            volume_5m=to_float((p.get("volume") or {}).get("m5")),
            buys_5m=to_int(txns_m5.get("buys")),
            sells_5m=to_int(txns_m5.get("sells")),
            price_usd=to_float(p.get("priceUsd")),
            created_at=to_datetime(p.get("pairCreatedAt")),
            dexscreener_url=p.get("url") or f"https://dexscreener.com/{chain}/{addr}",
            fomo_url=self.fomo_tpl.format(chain=chain, address=addr),
            source="dexscreener",
        )
