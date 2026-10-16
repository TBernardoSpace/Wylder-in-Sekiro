"""Pre-filtro léxico (Levenshtein vía rapidfuzz). Barato: se ejecuta para cada par token × noticia
y solo los pares que superan FUZZY_PREFILTER llegan al LLM."""
from __future__ import annotations

import re

from rapidfuzz import fuzz

from ..models import NewsItem, TokenInfo
from ..utils import norm_alnum, significant_words


def fuzzy_score(token: TokenInfo, news: NewsItem) -> tuple[float, str]:
    sym = norm_alnum(token.symbol)
    name_n = norm_alnum(token.name)
    tickers = [norm_alnum(t) for t in news.potential_tickers if norm_alnum(t)]
    best, why = 0.0, ""

    # 1) Ticker del token vs tickers potenciales (comparación estricta: los tickers son cortos)
    for t in tickers:
        s = _ticker_sim(sym, t)
        if s > best:
            best, why = s, f"ticker ${token.symbol} ≈ {t}"
        s2 = _ticker_sim(name_n, t)
        if s2 > best:
            best, why = s2, f"nombre '{token.name}' ≈ {t}"

    # 2) Cobertura de las palabras del nombre por keywords/titular
    name_words = significant_words(token.name) or significant_words(token.symbol)
    pool = set(significant_words(" ".join(news.keywords)) + significant_words(news.headline))
    if name_words and pool:
        hits = [w for w in name_words if any(fuzz.ratio(w, p) >= 85 for p in pool)]
        coverage = len(hits) / len(name_words)
        # un único término compartido (p.ej. solo "trump") no debe bastar: se penaliza
        s = coverage * (95.0 if len(hits) >= 2 else 70.0)
        if s > best:
            best, why = s, f"palabras del nombre en la noticia: {', '.join(hits)}"

    # 3) Símbolo como substring del titular normalizado ("PENNY" dentro de "TRUMP...PENNY...")
    if len(sym) >= 4 and sym in norm_alnum(news.headline) and best < 88:
        best, why = 88.0, f"${token.symbol} aparece en el titular"

    # 4) Descripción del creador (pump.fun/IPFS) vs noticia: varias palabras significativas en común
    if token.description and pool:
        desc_words = set(significant_words(token.description[:600]))
        shared = sorted(w for w in desc_words if w in pool and len(w) >= 4)
        if len(shared) >= 2:
            s = min(70.0 + 6 * (len(shared) - 2), 90.0)
            if s > best:
                best, why = s, f"la descripción del token menciona: {', '.join(shared[:5])}"

    # 5) El token enlaza la noticia o el mismo tweet
    if news.url:
        ref = _link_key(news.url)
        for link in (token.twitter, token.website, token.telegram):
            if link and ref and _link_key(link) == ref:
                return 100.0, "el token enlaza directamente la noticia/tweet"

    return round(best, 1), why


def _ticker_sim(a: str, b: str) -> float:
    """Levenshtein con dos salvaguardas: en tickers cortos (<= 8) una letra distinta ya es OTRO
    ticker (TDOLLAR != EDOLLAR), y si las cifras difieren es otra cosa (1DOLLAR != 12DOLLAR)."""
    if a == b:
        return 100.0
    s = fuzz.ratio(a, b)
    if re.sub(r"\D", "", a) != re.sub(r"\D", "", b):
        s = min(s, 60.0)
    if max(len(a), len(b)) <= 8:
        s = min(s, 75.0)
    return s


def _link_key(url: str) -> str:
    """Clave comparable de un link: id del tweet si es de X/Twitter, si no host+ruta sin query."""
    m = re.search(r"(?:twitter|x)\.com/[^/]+/status(?:es)?/(\d+)", url or "")
    if m:
        return f"tweet:{m.group(1)}"
    u = re.sub(r"^https?://(www\.)?", "", (url or "").split("?")[0].split("#")[0]).rstrip("/").lower()
    return u if "/" in u else ""  # un dominio pelado (p.ej. "x.com") no identifica una noticia
