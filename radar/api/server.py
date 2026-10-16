"""API + WebSocket del dashboard. Sirve web/index.html y emite cada evento del bus en vivo."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

from .. import bus as topics
from ..app import Radar
from ..config import Settings

log = logging.getLogger(__name__)
WEB_DIR = Path(__file__).resolve().parents[2] / "web"


def _serialize(topic: str, payload) -> dict:
    if topic == topics.SIGNAL:
        data = payload.dump()
    elif hasattr(payload, "model_dump"):
        data = payload.model_dump(mode="json")
    else:
        data = payload
    return {"type": topic, "data": data}


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    radar = Radar(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await radar.start()
        yield
        await radar.stop()

    app = FastAPI(title="News-to-Token Signal Radar", lifespan=lifespan)
    app.state.radar = radar

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html")

    @app.get("/health")
    async def health() -> dict:
        return {"ok": True, "status": radar.status}

    @app.get("/api/snapshot")
    async def snapshot() -> JSONResponse:
        return JSONResponse(radar.snapshot())

    @app.get("/api/signals")
    async def signals(limit: int = 50) -> JSONResponse:
        return JSONResponse(await radar.storage.recent_signals(min(limit, 500)))

    @app.post("/api/ingest/fomo")
    async def ingest_fomo(request: Request, x_radar_token: str | None = Header(default=None)) -> dict:
        """Recibe {url, payload} desde tools/fomo_bridge.user.js (corre dentro de fomo.family)."""
        if settings.ingest_token and x_radar_token != settings.ingest_token:
            raise HTTPException(status_code=401, detail="INGEST_TOKEN inválido")
        body = await request.json()
        if not isinstance(body, dict) or "payload" not in body:
            raise HTTPException(status_code=422, detail="se espera {url, payload}")
        n = await radar.ingest_fomo_payload(str(body.get("url", "")), body["payload"])
        return {"accepted": n}

    @app.websocket("/ws")
    async def ws(socket: WebSocket) -> None:
        await socket.accept()
        q = radar.bus.subscribe(maxsize=500, lossy=True)
        try:
            await socket.send_json({"type": "snapshot", "data": radar.snapshot()})
            while True:
                topic, payload = await q.get()
                await socket.send_json(_serialize(topic, payload))
        except (WebSocketDisconnect, asyncio.CancelledError):
            pass
        except Exception as exc:  # noqa: BLE001 - cliente cerró a mitad de envío, etc.
            log.debug("WS cerrado: %s", exc)
        finally:
            radar.bus.unsubscribe(q)

    return app
