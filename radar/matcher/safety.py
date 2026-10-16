"""Filtro de seguridad on-chain: liquidez mínima, volumen 5m creciente y anti-honeypot."""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass

from ..config import Settings
from ..models import SafetyReport, TokenInfo
from ..onchain.security import SecurityChecker, SecurityResult
from ..utils import human_usd


@dataclass
class Snapshot:
    t: float
    volume_5m: float | None
    liquidity: float | None


class MetricsHistory:
    """Guarda snapshots por token para medir si el volumen 5m está creciendo."""

    def __init__(self, maxlen: int = 30) -> None:
        self._h: dict[str, deque[Snapshot]] = {}
        self.maxlen = maxlen

    def record(self, token: TokenInfo) -> None:
        dq = self._h.setdefault(token.key, deque(maxlen=self.maxlen))
        snap = Snapshot(time.monotonic(), token.volume_5m, token.liquidity_usd)
        if dq and snap.t - dq[-1].t < 3 and dq[-1].volume_5m == snap.volume_5m:
            return  # mismo dato repetido
        dq.append(snap)

    def trend(self, key: str, min_growth_pct: float, min_gap_s: float = 10.0) -> str:
        dq = self._h.get(key)
        if not dq or len(dq) < 2:
            return "unknown"
        last = dq[-1]
        prev = next((s for s in reversed(dq) if last.t - s.t >= min_gap_s and s.volume_5m is not None), None)
        if prev is None or last.volume_5m is None:
            return "unknown"
        if prev.volume_5m == 0:
            return "up" if last.volume_5m > 0 else "flat"
        growth = (last.volume_5m - prev.volume_5m) / prev.volume_5m * 100
        if growth > min_growth_pct:
            return "up"
        return "down" if growth < -5 else "flat"


class SafetyFilter:
    def __init__(self, settings: Settings, history: MetricsHistory, security: SecurityChecker | None) -> None:
        self.s, self.history, self.security = settings, history, security

    async def evaluate(self, token: TokenInfo) -> SafetyReport:
        s = self.s
        reasons: list[str] = []
        warnings: list[str] = []

        liq_ok = token.liquidity_usd is not None and token.liquidity_usd >= s.min_liquidity_usd
        if not liq_ok:
            reasons.append(f"liquidez {human_usd(token.liquidity_usd)} < {human_usd(s.min_liquidity_usd)}")

        trend = self.history.trend(token.key, s.volume_growth_min_pct, s.trend_min_gap_s)
        vol_abs_ok = token.volume_5m is not None and token.volume_5m >= s.min_volume_5m
        # Con un solo snapshot usamos presión compradora (buys >= sells) como proxy de crecimiento
        buy_pressure = token.buys_5m is not None and token.sells_5m is not None and token.buys_5m >= token.sells_5m
        if not vol_abs_ok:
            reasons.append(f"volumen 5m {human_usd(token.volume_5m)} < {human_usd(s.min_volume_5m)}")
            vol_ok = False
        elif trend == "up":
            vol_ok = True
        elif trend == "unknown":
            vol_ok = not s.require_volume_growth or buy_pressure
            (warnings if vol_ok else reasons).append(
                "tendencia de volumen aún desconocida" + (" (buys ≥ sells)" if buy_pressure else ""))
        else:
            vol_ok = not s.require_volume_growth
            (warnings if vol_ok else reasons).append(f"volumen 5m {trend}")

        honeypot, hard_fail = "skipped", False
        if s.honeypot_policy != "off":
            res: SecurityResult = (await self.security.check(token) if self.security
                                   else SecurityResult("unknown", ["sin verificador"]))
            if res.holders and not token.holder_count:
                token.holder_count = res.holders
            honeypot = res.verdict
            if res.verdict == "danger":
                hard_fail = True
                reasons.extend(res.details)
            elif res.verdict == "unknown":
                if token.sells_5m:  # ventas reales observadas on-chain => al menos se puede vender
                    warnings.append(f"seguridad no verificada, pero hay {token.sells_5m} ventas/5m")
                elif s.honeypot_policy == "strict":
                    reasons.append("no se pudo verificar que se pueda vender")
                else:
                    warnings.append("⚠️ venta NO verificada (" + "; ".join(res.details) + ")")
            else:
                warnings.extend(d for d in res.details if d.startswith("⚠️"))

        if s.min_fomo_buyers_5m and (token.fomo_buyers_5m or 0) < s.min_fomo_buyers_5m:
            reasons.append(f"{token.fomo_buyers_5m or 0} compradores FOMO/5m < {s.min_fomo_buyers_5m}")

        if s.require_on_fomo and not token.on_fomo:
            reasons.append("aún no visto en fomo.family (REQUIRE_ON_FOMO)")

        passed = liq_ok and vol_ok and not hard_fail and not reasons
        return SafetyReport(passed=passed, hard_fail=hard_fail, liquidity_ok=liq_ok, volume_ok=vol_ok,
                            volume_trend=trend, honeypot=honeypot,  # type: ignore[arg-type]
                            reasons=reasons, warnings=warnings)
