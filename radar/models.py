"""Modelos de datos que viajan por el bus de eventos, la base de datos y el dashboard."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, computed_field

from .utils import utcnow

EVM_CHAINS = {"ethereum", "base", "bsc", "arbitrum", "polygon", "avalanche", "blast", "optimism"}


class NewsItem(BaseModel):
    id: str  # hash del titular normalizado (dedupe entre fuentes)
    headline: str
    summary: str = ""
    source: str
    url: str | None = None
    published_at: datetime
    received_at: datetime = Field(default_factory=utcnow)
    keywords: list[str] = Field(default_factory=list)
    potential_tickers: list[str] = Field(default_factory=list)
    narrative: str = ""
    meme_potential: int | None = None
    extractor: Literal["llm", "heuristic", "demo", "cluster"] = "heuristic"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def timestamp(self) -> str:
        return self.published_at.isoformat()


class TokenInfo(BaseModel):
    name: str
    symbol: str
    token_address: str
    chain: str
    market_cap: float | None = None
    liquidity_usd: float | None = None
    volume_5m: float | None = None
    holder_count: int | None = None
    buys_5m: int | None = None
    sells_5m: int | None = None
    price_usd: float | None = None
    created_at: datetime | None = None
    fomo_url: str | None = None
    dexscreener_url: str | None = None
    source: str = "unknown"
    on_fomo: bool = False
    # Flujo de compras hechas DESDE la app FOMO (Vía D, on-chain), ventana deslizante de 5 min
    fomo_buys_5m: int | None = None
    fomo_sells_5m: int | None = None
    fomo_buyers_5m: int | None = None
    fomo_volume_usd_5m: float | None = None
    # Metadata del creador (pump.fun / IPFS): clave para cruzar con la noticia
    description: str | None = None
    twitter: str | None = None
    telegram: str | None = None
    website: str | None = None
    image_url: str | None = None
    creator: str | None = None
    first_seen: datetime = Field(default_factory=utcnow)
    last_update: datetime = Field(default_factory=utcnow)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def key(self) -> str:
        return token_key(self.chain, self.token_address)

    def merge_metrics(self, other: "TokenInfo") -> bool:
        """Copia métricas no nulas de `other`. Devuelve True si algo cambió."""
        changed = False
        for f in ("market_cap", "liquidity_usd", "volume_5m", "holder_count", "buys_5m", "sells_5m",
                  "price_usd", "created_at", "dexscreener_url", "fomo_buys_5m", "fomo_sells_5m",
                  "fomo_buyers_5m", "fomo_volume_usd_5m", "description", "twitter", "telegram", "website",
                  "image_url", "creator"):
            v = getattr(other, f)
            if v is not None and v != getattr(self, f):
                setattr(self, f, v)
                changed = True
        if other.on_fomo and not self.on_fomo:
            self.on_fomo = True
            self.fomo_url = other.fomo_url or self.fomo_url
            changed = True
        if changed:
            self.last_update = utcnow()
        return changed


def token_key(chain: str, address: str) -> str:
    chain = (chain or "").lower()
    addr = address.lower() if chain in EVM_CHAINS or address.startswith("0x") else address
    return f"{chain}:{addr}"


class SafetyReport(BaseModel):
    passed: bool
    hard_fail: bool = False  # True = descartado definitivamente (honeypot evidente)
    liquidity_ok: bool = False
    volume_ok: bool = False
    volume_trend: Literal["up", "flat", "down", "unknown"] = "unknown"
    honeypot: Literal["ok", "danger", "unknown", "skipped"] = "unknown"
    reasons: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


SignalStatus = Literal["confirmed", "secondary", "watch", "rejected"]


class Signal(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    news: NewsItem
    token: TokenInfo
    fuzzy_score: float
    llm_score: float | None = None
    match_score: float
    reason: str = ""
    safety: SafetyReport | None = None
    status: SignalStatus = "watch"
    is_leader: bool = False
    alerted: bool = False
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    def dump(self) -> dict[str, Any]:
        return self.model_dump(mode="json")
