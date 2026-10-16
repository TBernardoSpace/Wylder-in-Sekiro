"""Pre-filtro léxico (Levenshtein vía rapidfuzz). Barato: se ejecuta para cada par token × noticia
y solo los pares que superan FUZZY_PREFILTER llegan al LLM."""
from __future__ import annotations

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
        s = 100.0 if sym == t else fuzz.ratio(sym, t)
        if s > best:
            best, why = s, f"ticker ${token.symbol} ≈ {t}"
        s2 = 100.0 if name_n == t else fuzz.ratio(name_n, t)
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

    return round(best, 1), why
