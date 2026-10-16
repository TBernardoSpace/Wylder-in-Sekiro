"""Contrato del Token Scanner + normalizador de JSON arbitrario a TokenInfo.

Como fomo.family no publica un esquema oficial, cada vía devuelve JSON con nombres distintos.
`normalize_token` prueba una lista de rutas candidatas por campo; con FOMO_*_FIELD_MAP puedes
fijar la ruta exacta que veas en DevTools (ej. {"liquidity_usd": "stats.liquidity.usd"}).
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any, AsyncIterator

import httpx

from ..models import TokenInfo
from ..utils import find_item_list, first_path, get_path, to_datetime, to_float, to_int

log = logging.getLogger(__name__)

DEFAULT_FIELD_CANDIDATES: dict[str, list[str]] = {
    "name": ["name", "tokenName", "token.name", "baseToken.name", "metadata.name", "asset.name"],
    "symbol": ["symbol", "ticker", "tokenSymbol", "token.symbol", "baseToken.symbol", "metadata.symbol"],
    "token_address": ["address", "tokenAddress", "token_address", "mint", "mintAddress", "contract",
                      "contractAddress", "ca", "token.address", "baseToken.address"],
    "chain": ["chain", "chainId", "chain_id", "network", "blockchain", "token.chain"],
    "market_cap": ["marketCap", "market_cap", "marketCapUsd", "mcap", "usd_market_cap", "fdv",
                   "stats.marketCap", "token.marketCap"],
    "liquidity_usd": ["liquidity.usd", "liquidityUsd", "liquidity_usd", "liquidity", "stats.liquidity",
                      "token.liquidity"],
    "volume_5m": ["volume.m5", "volume5m", "volume_5m", "volume5mUsd", "stats.volume5m", "volume.5m",
                  "stats.5m.volume"],
    "holder_count": ["holders", "holderCount", "holder_count", "holdersCount", "stats.holders"],
    "buys_5m": ["txns.m5.buys", "buys5m", "stats.5m.buys"],
    "sells_5m": ["txns.m5.sells", "sells5m", "stats.5m.sells"],
    "price_usd": ["priceUsd", "price_usd", "price", "stats.price"],
    "created_at": ["createdAt", "created_at", "created_timestamp", "pairCreatedAt", "launchTime",
                   "mintedAt", "timestamp"],
    "fomo_url": ["url", "link", "shareUrl", "fomoUrl"],
}

CHAIN_ALIASES = {"sol": "solana", "solana": "solana", "base": "base", "8453": "base", "eth": "ethereum",
                 "ethereum": "ethereum", "1": "ethereum", "bsc": "bsc", "56": "bsc", "bnb": "bsc"}


def normalize_token(item: dict[str, Any], field_map: dict[str, str] | None, *, source: str,
                    default_chain: str, url_template: str, on_fomo: bool) -> TokenInfo | None:
    def pick(field: str) -> Any:
        if field_map and field in field_map:
            return get_path(item, field_map[field])
        return first_path(item, DEFAULT_FIELD_CANDIDATES[field])

    address = pick("token_address")
    symbol = pick("symbol")
    if not address or not symbol:
        return None
    address = str(address)
    chain_raw = str(pick("chain") or default_chain).lower()
    chain = CHAIN_ALIASES.get(chain_raw, chain_raw)
    if chain == default_chain and address.startswith("0x") and default_chain == "solana":
        chain = "base"  # dirección EVM sin cadena explícita: fomo opera Solana + Base
    liq = pick("liquidity_usd")
    if isinstance(liq, dict):
        liq = liq.get("usd")
    url = pick("fomo_url")
    fomo_url = url if isinstance(url, str) and "fomo" in url else url_template.format(chain=chain, address=address)
    return TokenInfo(
        name=str(pick("name") or symbol),
        symbol=str(symbol).lstrip("$"),
        token_address=address,
        chain=chain,
        market_cap=to_float(pick("market_cap")),
        liquidity_usd=to_float(liq),
        volume_5m=to_float(pick("volume_5m")),
        holder_count=to_int(pick("holder_count")),
        buys_5m=to_int(pick("buys_5m")),
        sells_5m=to_int(pick("sells_5m")),
        price_usd=to_float(pick("price_usd")),
        created_at=to_datetime(pick("created_at")),
        fomo_url=fomo_url,
        dexscreener_url=f"https://dexscreener.com/{chain}/{address}",
        source=source,
        on_fomo=on_fomo,
    )


class TokenSource(ABC):
    name: str = "tokens"
    on_fomo: bool = False  # True si los datos provienen de fomo.family (Vía A/B)

    @abstractmethod
    async def poll(self, client: httpx.AsyncClient) -> list[TokenInfo]: ...

    def stream(self) -> AsyncIterator[TokenInfo] | None:
        """Opcional: fuentes push (WebSocket / Playwright). Devuelve None si no aplica."""
        return None


class JsonHttpTokenSource(TokenSource):
    """Base reutilizable: endpoint HTTP que devuelve JSON con una lista de tokens."""

    def __init__(self, *, name: str, url: str, method: str = "GET", headers: dict | None = None,
                 params: dict | None = None, body: Any = None, items_path: str = "",
                 field_map: dict | None = None, default_chain: str = "solana",
                 url_template: str, on_fomo: bool = True) -> None:
        self.name, self.url, self.method = name, url, method
        self.headers, self.params, self.body = headers or {}, params or {}, body
        self.items_path, self.field_map = items_path, field_map or {}
        self.default_chain, self.url_template, self.on_fomo = default_chain, url_template, on_fomo

    async def poll(self, client: httpx.AsyncClient) -> list[TokenInfo]:
        r = await client.request(self.method, self.url, headers=self.headers, params=self.params or None,
                                 json=self.body if self.method != "GET" else None)
        r.raise_for_status()
        return self.parse_payload(r.json())

    def parse_payload(self, data: Any) -> list[TokenInfo]:
        items = get_path(data, self.items_path) if self.items_path else find_item_list(data)
        if not isinstance(items, list):
            log.warning("%s: no se encontró la lista de tokens (items_path=%r)", self.name, self.items_path)
            return []
        out = []
        for it in items:
            if not isinstance(it, dict):
                continue
            tok = normalize_token(it, self.field_map, source=self.name, default_chain=self.default_chain,
                                  url_template=self.url_template, on_fomo=self.on_fomo)
            if tok:
                out.append(tok)
        return out
