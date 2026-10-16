"""Modo demo offline (DEMO_MODE=true o `python -m radar --demo`).

Simula el caso "Trump anuncia moneda de 1 centavo": llega la noticia, aparecen ~6 memecoins
(una con liquidez real, varias basura y un honeypot) y sus métricas evolucionan. Sirve para ver el
dashboard, probar Telegram/Discord y validar el motor sin claves ni red.
"""
from __future__ import annotations

import asyncio
import random
from datetime import timedelta
from typing import AsyncIterator

import httpx

from .config import Settings
from .fomo.base import TokenSource
from .models import TokenInfo
from .news.base import RawNews, StreamingNewsSource
from .onchain.security import SecurityResult
from .utils import utcnow

DEMO_NEWS = [
    (2, "Fed's Waller says rate cuts remain on the table for December", "Reuters"),
    (4, "BREAKING: Trump announces new US '1 cent' commemorative coin — 'The Trump Penny'", "WatcherGuru"),
    (30, "Elon Musk posts picture of his dog wearing a tiny astronaut helmet", "Tree of Alpha"),
    (55, "Ethereum ETF sees record $1.2B daily inflows", "CoinDesk"),
]

# (nombre, ticker, liquidez inicial, volumen inicial, crecimiento/tick, honeypot, delay s)
DEMO_TOKENS = [
    ("Trump Penny", "PENNY", 42_000, 9_000, 1.25, False, 8),
    ("One Cent Trump", "1CENT", 1_800, 400, 1.05, False, 9),
    ("TrumpCoin Penny", "TPENNY", 9_000, 2_500, 1.02, True, 10),
    ("Penny Inu", "PINU", 600, 50, 1.0, False, 12),
    ("Random Frog", "RFROG", 15_000, 6_000, 1.1, False, 6),
    ("Space Dog Helmet", "HELMET", 25_000, 8_000, 1.3, False, 38),
    ("Trump Penny 2.0", "PENNY2", 120_000, 60_000, 1.2, False, 26),
]


class DemoNewsSource(StreamingNewsSource):
    name = "demo-news"

    async def stream(self) -> AsyncIterator[RawNews]:
        start = asyncio.get_running_loop().time()
        for delay, headline, src in DEMO_NEWS:
            await asyncio.sleep(max(0.0, start + delay - asyncio.get_running_loop().time()))
            yield RawNews(headline=headline, source=src, url="https://example.com/news",
                          published_at=utcnow() - timedelta(seconds=20))
        while True:
            await asyncio.sleep(3600)


class DemoTokenSource(TokenSource):
    name = "fomo:demo"
    on_fomo = True

    def __init__(self, s: Settings) -> None:
        self.s = s
        self.t0: float | None = None
        self.state: dict[str, dict] = {}

    async def poll(self, client: httpx.AsyncClient) -> list[TokenInfo]:
        loop = asyncio.get_running_loop()
        self.t0 = self.t0 or loop.time()
        elapsed = loop.time() - self.t0
        out = []
        for i, (name, sym, liq, vol, growth, _hp, delay) in enumerate(DEMO_TOKENS):
            if elapsed < delay:
                continue
            st = self.state.setdefault(sym, {"liq": liq, "vol": vol, "holders": random.randint(20, 80),
                                             "addr": f"Demo{sym}{i}" + "x" * (36 - len(sym)) + "pump"})
            st["liq"] *= 1 + (growth - 1) / 2
            st["vol"] *= growth * random.uniform(0.98, 1.04)
            st["holders"] += int(st["vol"] / 2000)
            buys = int(st["vol"] / 150)
            out.append(TokenInfo(
                name=name, symbol=sym, token_address=st["addr"], chain="solana",
                market_cap=st["liq"] * 6, liquidity_usd=round(st["liq"]), volume_5m=round(st["vol"]),
                holder_count=st["holders"], buys_5m=buys, sells_5m=int(buys * 0.6),
                created_at=utcnow() - timedelta(seconds=10),
                fomo_url=self.s.fomo_token_url_template.format(chain="solana", address=st["addr"]),
                dexscreener_url=f"https://dexscreener.com/solana/{st['addr']}",
                source=self.name, on_fomo=True,
            ))
        return out


class DemoSecurity:
    async def check(self, token: TokenInfo) -> SecurityResult:
        hp = {sym for _, sym, *_rest in DEMO_TOKENS if _rest[3]}
        if token.symbol in hp:
            return SecurityResult("danger", ["⛔ Freeze Authority still enabled"], provider="demo")
        return SecurityResult("ok", ["sin riesgos graves (demo)"], provider="demo")
