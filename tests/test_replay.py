from __future__ import annotations

from pathlib import Path

import pytest

from radar.config import Settings
from radar.matcher.token_search import pick_queries
from radar.models import NewsItem
from radar.news.extractor import heuristic_extract
from radar.replay import run_headless
from radar.utils import headline_hash, utcnow

SCENARIO = str(Path(__file__).resolve().parents[1] / "scenarios" / "dacity_gta6.json")


def test_pick_queries_prefers_specific_tickers():
    h = "Rockstar releases new GTA 6 trailer: a plane carries the 'DacityCoin AudaCity' livery"
    d = heuristic_extract(h)
    n = NewsItem(id=headline_hash(h), headline=h, source="t", published_at=utcnow(),
                 keywords=d["keywords"], potential_tickers=d["potential_tickers"])
    qs = [q.lower() for q in pick_queries(n, 4)]
    assert "dacitycoin" in qs


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", ["", "#sin-noticias"])
async def test_dacity_replay(variant):
    """El token original (creado 5 días antes del tráiler) debe ser la primera y única alerta buena:
    ni el honeypot ni las copias sin liquidez. Sin noticias, lo encuentra la narrativa emergente."""
    report, ok = await run_headless(Settings(llm_enabled=False), SCENARIO + variant, speed=600)
    assert ok, report
    assert "SimCopia2Honeypot" not in report.split("── Resultado ──")[1]
