"""Arma un escenario de replay con datos REALES de DexScreener (córrelo en tu máquina, con internet).

Ejemplo, para tu moneda de 1 dólar de Trump:
    python tools/build_scenario.py \\
        --mint 2bCCHQadi4QeHWPKG3w13siCyWWQPgkFtniouTqVpump \\
        --query dollar --query "trump dollar" --query 1dollar \\
        --out scenarios/mi_trump_dollar.json
    python -m radar --replay "scenarios/mi_trump_dollar.json#sin-noticias" --headless

Qué es real y qué no:
  * Nombre, ticker, dirección y fecha de creación de cada token salen de DexScreener (reales).
  * DexScreener no da series históricas: las métricas de la línea de tiempo son las ACTUALES,
    repartidas con una rampa desde el lanzamiento. Sirve para probar detección y elección del líder
    (quién concentra la liquidez), no para medir el precio de entrada.
  * Si conoces el titular y la hora de la noticia: --headline "..." --news-time "2026-09-02T14:05Z".
    Sin noticia, el replay prueba el camino "narrativa emergente + búsqueda inversa".
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from radar.onchain.dexscreener import DexScreenerClient  # noqa: E402
from radar.utils import to_datetime  # noqa: E402


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mint", required=True, help="token que crees que fue el ganador")
    ap.add_argument("--query", action="append", default=[], help="términos para encontrar competidores")
    ap.add_argument("--headline", default="")
    ap.add_argument("--news-time", default="", help="ISO-8601 de la noticia (si la sabes)")
    ap.add_argument("--window-h", type=float, default=24, help="competidores creados ±N horas del ganador")
    ap.add_argument("--out", default="scenarios/mi_escenario.json")
    args = ap.parse_args()

    async with httpx.AsyncClient(timeout=15) as client:
        dex = DexScreenerClient(client, "https://fomo.family/tokens/{chain}/{address}")
        winner = (await dex.tokens("solana", [args.mint]) or [None])[0]
        if not winner:
            sys.exit(f"DexScreener no conoce {args.mint}")
        found = {winner.token_address: winner}
        for q in args.query or [winner.symbol, winner.name]:
            for t in await dex.search(q):
                if t.chain == "solana":
                    found.setdefault(t.token_address, t)

    w_created = winner.created_at or datetime.now(timezone.utc)
    t0 = to_datetime(args.news_time) if args.news_time else w_created
    tokens = []
    for t in found.values():
        if not t.created_at or abs((t.created_at - w_created).total_seconds()) > args.window_h * 3600:
            continue
        created_min = round((t.created_at - t0).total_seconds() / 60, 1)
        ramp = [{"t": created_min, **_scale(t, 0.05)}, {"t": max(created_min, 0) + 5, **_scale(t, 0.4)},
                {"t": max(created_min, 0) + 20, **_scale(t, 1.0)}]
        tokens.append({"name": t.name, "symbol": t.symbol, "address": t.token_address, "chain": "solana",
                       "created_min": created_min, "timeline": ramp,
                       "note": "métricas = valores ACTUALES de DexScreener con rampa (no históricas)"})
    scenario = {
        "name": f"{winner.name} (${winner.symbol}) — datos reales de DexScreener",
        "description": "Generado por tools/build_scenario.py. Tokens reales; métricas actuales con rampa.",
        "speed": 30,
        "news": [{"t": 0, "headline": args.headline, "source": "usuario"}] if args.headline else [],
        "tokens": sorted(tokens, key=lambda x: x["created_min"]),
        "expect": {"first_alert": winner.token_address, "within_min": 30,
                   "first_alert_without_news": winner.token_address, "within_min_without_news": 60},
    }
    Path(args.out).write_text(json.dumps(scenario, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"{len(tokens)} tokens → {args.out}")
    for tk in scenario["tokens"]:
        print(f"  {tk['created_min']:>9.1f} min  ${tk['symbol']:<12} {tk['name'][:30]:<30} {tk['address']}")


def _scale(t, k: float) -> dict:
    def f(v):
        return round(v * k, 2) if v is not None else None
    # Pasado el pico el volumen 5m actual suele ser ~0: se usa un proxy (30% de la liquidez) para que
    # el replay represente el momento del pump y no el de hoy.
    vol = t.volume_5m if (t.volume_5m or 0) >= 1000 else (t.liquidity_usd or 0) * 0.3
    buys = t.buys_5m if (t.buys_5m or 0) >= 10 else int(vol / 200)
    return {"market_cap": f(t.market_cap), "liquidity_usd": f(t.liquidity_usd), "volume_5m": f(vol),
            "buys_5m": int(buys * k), "sells_5m": int(buys * k * 0.5)}


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
