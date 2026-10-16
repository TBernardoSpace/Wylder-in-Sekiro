"""Chequeos anti-honeypot / anti-rug.

- Solana: RugCheck (`/v1/tokens/{mint}/report/summary`): mint/freeze authority activas, LP sin lockear, etc.
  Un freeze authority activo es el "honeypot" típico de Solana (el creador puede congelar tu cuenta).
- EVM (Base, ETH, BSC): honeypot.is (`/v2/IsHoneypot`), que simula compra+venta y devuelve impuestos.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

import httpx

from ..models import TokenInfo
from ..utils import to_float, to_int

log = logging.getLogger(__name__)

EVM_CHAIN_IDS = {"ethereum": 1, "bsc": 56, "base": 8453, "arbitrum": 42161}


@dataclass
class SecurityResult:
    verdict: str  # ok | danger | unknown
    details: list[str] = field(default_factory=list)
    holders: int | None = None
    provider: str = ""


class SecurityChecker:
    def __init__(self, client: httpx.AsyncClient, cache_ttl_s: float = 90.0) -> None:
        self.client = client
        self.ttl = cache_ttl_s
        self._cache: dict[str, tuple[float, SecurityResult]] = {}

    async def check(self, token: TokenInfo) -> SecurityResult:
        hit = self._cache.get(token.key)
        if hit and time.monotonic() - hit[0] < self.ttl:
            return hit[1]
        try:
            if token.chain == "solana":
                res = await self._rugcheck(token.token_address)
            elif token.chain in EVM_CHAIN_IDS:
                res = await self._honeypot_is(token.token_address, EVM_CHAIN_IDS[token.chain])
            else:
                res = SecurityResult("unknown", [f"sin proveedor de seguridad para {token.chain}"])
        except (httpx.HTTPError, ValueError) as exc:
            log.info("Chequeo de seguridad falló para %s: %s", token.key, exc)
            res = SecurityResult("unknown", [f"proveedor no respondió: {type(exc).__name__}"])
        self._cache[token.key] = (time.monotonic(), res)
        return res

    async def _rugcheck(self, mint: str) -> SecurityResult:
        r = await self.client.get(f"https://api.rugcheck.xyz/v1/tokens/{mint}/report/summary", timeout=6)
        if r.status_code in (400, 404):
            return SecurityResult("unknown", ["RugCheck aún no indexó el token"], provider="rugcheck")
        r.raise_for_status()
        data = r.json()
        dangers = [x.get("name", "?") for x in data.get("risks", []) if x.get("level") == "danger"]
        warns = [x.get("name", "?") for x in data.get("risks", []) if x.get("level") == "warn"]
        details = [f"⛔ {d}" for d in dangers] + [f"⚠️ {w}" for w in warns[:3]]
        # Freeze/mint authority activas => no se puede garantizar la venta
        verdict = "danger" if dangers else "ok"
        holders = to_int(data.get("totalHolders"))
        return SecurityResult(verdict, details or ["RugCheck sin riesgos graves"], holders, "rugcheck")

    async def _honeypot_is(self, address: str, chain_id: int) -> SecurityResult:
        r = await self.client.get(
            "https://api.honeypot.is/v2/IsHoneypot", params={"address": address, "chainID": chain_id}, timeout=8
        )
        r.raise_for_status()
        data = r.json()
        is_hp = (data.get("honeypotResult") or {}).get("isHoneypot")
        sim = data.get("simulationResult") or {}
        sell_tax = to_float(sim.get("sellTax"))
        details = []
        if sell_tax is not None:
            details.append(f"sell tax {sell_tax:.1f}%")
        holders = to_int((data.get("holderAnalysis") or {}).get("holders"))
        if is_hp is True or (sell_tax is not None and sell_tax >= 50):
            return SecurityResult("danger", ["⛔ honeypot.is: no se puede vender"] + details, holders, "honeypot.is")
        if is_hp is None:
            return SecurityResult("unknown", ["honeypot.is sin simulación"] + details, holders, "honeypot.is")
        return SecurityResult("ok", ["venta simulada OK"] + details, holders, "honeypot.is")
