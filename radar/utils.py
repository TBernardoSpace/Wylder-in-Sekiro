"""Utilidades compartidas: tiempo, normalización de texto y acceso a JSON por rutas."""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any, Iterable

# Palabras demasiado genéricas en nombres de memecoins / titulares: no deben generar matches por sí solas.
GENERIC_WORDS = {
    "the", "a", "an", "of", "to", "in", "on", "for", "and", "or", "is", "are", "was", "be", "by", "with",
    "at", "as", "from", "it", "its", "this", "that", "new", "says", "said", "after", "over", "will", "just",
    "coin", "token", "official", "meme", "memecoin", "inu", "sol", "solana", "base", "eth", "crypto",
    "moon", "pump", "fun", "ai", "dao", "finance", "protocol", "network", "chain", "cash", "money",
    "breaking", "news", "report", "reports", "update", "live", "today", "now", "us", "usa",
    "el", "la", "los", "las", "de", "del", "en", "y", "que", "un", "una", "por", "con", "para", "anuncia",
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_datetime(value: Any) -> datetime | None:
    """Convierte epoch (s o ms), ISO-8601 o formatos GDELT (20261016T110000Z) a datetime UTC."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e12:  # milisegundos
            ts /= 1000.0
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    s = str(value).strip()
    if s.isdigit():
        return to_datetime(int(s))
    m = re.fullmatch(r"(\d{8})T(\d{6})Z", s)
    if m:
        return datetime.strptime(s, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime

        return parsedate_to_datetime(s).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def to_float(value: Any) -> float | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        return float(str(value).replace(",", "").replace("$", ""))
    except ValueError:
        return None


def to_int(value: Any) -> int | None:
    f = to_float(value)
    return int(f) if f is not None else None


def get_path(data: Any, path: str) -> Any:
    """Acceso por ruta con puntos: "data.items.0.name". Devuelve None si no existe."""
    if not path:
        return data
    cur = data
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.lstrip("-").isdigit():
            idx = int(part)
            cur = cur[idx] if -len(cur) <= idx < len(cur) else None
        else:
            return None
        if cur is None:
            return None
    return cur


def first_path(data: Any, paths: Iterable[str]) -> Any:
    for p in paths:
        v = get_path(data, p)
        if v not in (None, ""):
            return v
    return None


def find_item_list(data: Any) -> list[dict]:
    """Si no se conoce la ruta de la lista de items, busca la lista de dicts más grande del JSON."""
    if isinstance(data, list) and (not data or isinstance(data[0], dict)):
        return data
    best: list[dict] = []
    stack = [data]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            stack.extend(cur.values())
        elif isinstance(cur, list):
            if cur and all(isinstance(x, dict) for x in cur) and len(cur) > len(best):
                best = cur
            stack.extend(x for x in cur if isinstance(x, (dict, list)))
    return best


def norm_alnum(s: str) -> str:
    """'$Trump-Penny 1¢' -> 'TRUMPPENNY1'."""
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def significant_words(text: str) -> list[str]:
    words = re.findall(r"[A-Za-z0-9]+", (text or "").lower())
    return [w for w in words if len(w) >= 3 and w not in GENERIC_WORDS]


def headline_hash(headline: str) -> str:
    """Hash estable para deduplicar la misma noticia publicada por varias fuentes."""
    key = " ".join(re.findall(r"[a-z0-9]+", headline.lower()))
    return hashlib.sha1(key.encode()).hexdigest()[:16]


def human_usd(v: float | None) -> str:
    if v is None:
        return "n/d"
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(v) >= div:
            return f"${v / div:.2f}{suf}"
    return f"${v:,.0f}"


def minutes_ago(dt: datetime | None) -> str:
    if dt is None:
        return "n/d"
    secs = (utcnow() - dt).total_seconds()
    if secs < 60:
        return f"hace {int(max(secs, 0))} s"
    if secs < 3600:
        return f"hace {int(secs // 60)} min"
    return f"hace {secs / 3600:.1f} h"
