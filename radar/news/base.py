"""Contratos de las fuentes de noticias."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import AsyncIterator

import httpx

from ..utils import utcnow


@dataclass
class RawNews:
    headline: str
    source: str
    url: str | None = None
    summary: str = ""
    published_at: datetime = field(default_factory=utcnow)


class PollingNewsSource(ABC):
    """Fuente que se consulta cada N segundos (REST / RSS)."""

    name: str = "polling"

    @abstractmethod
    async def fetch(self, client: httpx.AsyncClient) -> list[RawNews]: ...


class StreamingNewsSource(ABC):
    """Fuente push (WebSocket). Debe reconectar por sí misma."""

    name: str = "stream"

    @abstractmethod
    def stream(self) -> AsyncIterator[RawNews]: ...
