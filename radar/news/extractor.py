"""Extracción de entidades / tickers potenciales de un titular.

Estrategia: LLM (Claude Haiku) cuando está disponible + heurística determinista que SIEMPRE corre.
Los tickers de ambos se fusionan: la heurística cubre los literales obvios aunque el LLM falle o tarde.
"""
from __future__ import annotations

import logging
import re

from ..llm import LLMClient
from ..models import NewsItem
from ..utils import GENERIC_WORDS, headline_hash, significant_words, utcnow
from .base import RawNews

log = logging.getLogger(__name__)

# unidad tras una cifra -> sufijo de ticker ("1 cent" -> 1CENT)
_UNITS = {"cent": "CENT", "cents": "CENT", "centavo": "CENT", "centavos": "CENT", "penny": "PENNY",
          "dollar": "DOLLAR", "dollars": "DOLLAR", "dolar": "DOLLAR", "dólar": "DOLLAR", "dolares": "DOLLAR",
          "billion": "B", "million": "M", "trillion": "T", "percent": "PCT", "x": "X", "k": "K", "bps": "BPS"}
_NUM_WORDS = {"one": "1", "two": "2", "three": "3", "ten": "10", "hundred": "100", "thousand": "1000",
              "un": "1", "uno": "1", "dos": "2", "tres": "3", "diez": "10", "cien": "100", "mil": "1000"}
_MEME_OBJECTS = {"penny": "PENNY", "cent": "CENT", "cents": "CENT", "centavo": "CENT", "pennies": "PENNY"}


def heuristic_extract(headline: str, summary: str = "") -> dict:
    text = headline.strip()
    tokens = re.findall(r"\$?[^\W\d_][\w'’\-]*|\$?\d+(?:[.,]\d+)?[%¢$kKmMbB]?", text)
    keywords: list[str] = []
    tickers: list[str] = []

    # 1) Cashtags y hashtags explícitos: $PENNY, #TRUMPCOIN
    for tag in re.findall(r"[$#]([A-Za-z][A-Za-z0-9]{1,14})", f"{text} {summary}"):
        tickers.append(tag.upper())
        keywords.append(tag)

    # 2) Frases entre comillas: "Make Crypto Great Again"
    for phrase in re.findall(r"[\"“'‘]([^\"”'’]{3,40})[\"”'’]", text):
        keywords.append(phrase)
        words = re.findall(r"[A-Za-z0-9]+", phrase)
        acronym = "".join(w[0] for w in words).upper()
        if 2 <= len(acronym) <= 6:
            tickers.append(acronym)

    # 3) Nombres propios (secuencias Capitalizadas) y palabras en MAYÚSCULAS
    proper: list[str] = []
    for i, tok in enumerate(tokens):
        clean = tok.lstrip("$").replace("’", "'").split("'")[0].strip("-")
        if not clean or clean.lower() in GENERIC_WORDS or len(clean) < 2:
            continue
        if clean.isupper() and 2 <= len(clean) <= 10 and clean.isalpha():
            proper.append(clean)
        elif clean[0].isupper() and clean.isalpha() and (i > 0 or len(clean) > 3):
            proper.append(clean)
    for p in proper:
        keywords.append(p)
        if len(p) <= 10:
            tickers.append(p.upper())

    # 4) Cifras con unidad: "1 cent", "1 centavo", "100x" -> 1CENT, 100X
    objects: list[str] = []  # "ganchos" de la noticia, para combinar con el sujeto
    lowered = [t.lower().strip("'’-") for t in tokens]
    for i, tok in enumerate(lowered):
        num = _NUM_WORDS.get(tok, tok if re.fullmatch(r"\$?\d+(?:[.,]\d+)?[%¢kmb]?", tok) else None)
        if num is None:
            continue
        num_clean = re.sub(r"[^0-9]", "", num)
        nxt = lowered[i + 1] if i + 1 < len(lowered) else ""
        if nxt in _UNITS:
            unit = _UNITS[nxt]
            keywords.append(f"{num_clean} {nxt}")
            tickers.append(f"{num_clean}{unit}")
            objects += [f"{num_clean}{unit[0]}", unit]
        elif num.startswith("$"):  # "$1 coin" -> 1DOLLAR
            keywords.append(f"{num_clean} dollar")
            tickers.append(f"{num_clean}DOLLAR")
            objects += [f"{num_clean}D", "DOLLAR"]
        elif num.endswith("¢"):
            keywords.append(f"{num_clean} cent")
            tickers.append(f"{num_clean}CENT")
            objects.append(f"{num_clean}C")

    # 5) Objetos memeables sueltos (penny, cent...) también son ganchos
    for tok in lowered:
        if tok in _MEME_OBJECTS:
            keywords.append(tok)
            objects.append(_MEME_OBJECTS[tok])

    # 6) Combinaciones sujeto + gancho: TRUMP + PENNY -> TRUMPPENNY, TPENNY
    objects += [p.upper() for p in proper[1:]]
    tickers += objects
    if proper:
        subj = proper[0].upper()
        for o in [o for o in _dedupe(objects) if o != subj][:3]:
            if len(subj) + len(o) <= 12:
                tickers.append(f"{subj}{o}")
            tickers.append(f"{subj[0]}{o}"[:10])

    # 7) Resto de palabras significativas: solo keywords (no tickers, para no inflar el fuzzy)
    keywords += sorted((w for w in significant_words(text) if len(w) >= 4), key=len, reverse=True)[:6]

    return {
        "keywords": _dedupe(keywords)[:12],
        "potential_tickers": [t for t in _dedupe(tickers) if 2 <= len(t) <= 12][:15],
        "narrative": " ".join(_dedupe(proper)[:4]),
        "meme_potential": None,
    }


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out = []
    for it in items:
        k = it.lower().strip()
        if k and k not in seen:
            seen.add(k)
            out.append(it.strip())
    return out


class EntityExtractor:
    def __init__(self, llm: LLMClient | None) -> None:
        self.llm = llm

    async def process(self, raw: RawNews) -> NewsItem:
        heur = heuristic_extract(raw.headline, raw.summary)
        data, extractor = heur, "heuristic"
        if self.llm:
            llm_data = await self.llm.extract(raw.headline, raw.summary)
            if llm_data:
                extractor = "llm"
                data = {
                    "keywords": _dedupe(llm_data["keywords"] + heur["keywords"])[:12],
                    # primero los del LLM (mejor calidad), luego los literales heurísticos
                    "potential_tickers": _dedupe(llm_data["potential_tickers"] + heur["potential_tickers"])[:20],
                    "narrative": llm_data.get("narrative", ""),
                    "meme_potential": llm_data.get("meme_potential"),
                }
        return NewsItem(
            id=headline_hash(raw.headline),
            headline=raw.headline,
            summary=raw.summary,
            source=raw.source,
            url=raw.url,
            published_at=raw.published_at,
            received_at=utcnow(),
            keywords=data["keywords"],
            potential_tickers=data["potential_tickers"],
            narrative=data.get("narrative") or "",
            meme_potential=data.get("meme_potential"),
            extractor=extractor,  # type: ignore[arg-type]
        )
