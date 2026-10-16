"""Bus de eventos asíncrono (pub/sub en memoria) que desacopla ingestores, motor y frontend."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

log = logging.getLogger(__name__)

# Tópicos publicados
NEWS = "news"                  # NewsItem procesado (con entidades)
TOKEN = "token"                # TokenInfo nuevo
TOKEN_UPDATE = "token_update"  # TokenInfo con métricas actualizadas
SIGNAL = "signal"              # Signal (match) nuevo o actualizado
STATUS = "status"              # estado de los conectores

Event = tuple[str, Any]


class EventBus:
    def __init__(self) -> None:
        self._subs: dict[asyncio.Queue[Event], bool] = {}

    def subscribe(self, maxsize: int = 0, lossy: bool = False) -> asyncio.Queue[Event]:
        """Consumidores internos: maxsize=0 (sin pérdida). Clientes WS: lossy=True con tope."""
        q: asyncio.Queue[Event] = asyncio.Queue(maxsize=maxsize)
        self._subs[q] = lossy
        return q

    def unsubscribe(self, q: asyncio.Queue[Event]) -> None:
        self._subs.pop(q, None)

    def publish(self, topic: str, payload: Any) -> None:
        for q, lossy in list(self._subs.items()):
            try:
                q.put_nowait((topic, payload))
            except asyncio.QueueFull:
                if lossy:  # cliente lento: descarta el evento más viejo
                    try:
                        q.get_nowait()
                        q.put_nowait((topic, payload))
                    except (asyncio.QueueEmpty, asyncio.QueueFull):
                        pass
                else:
                    log.warning("Cola interna llena, evento %s descartado", topic)
