"""Vía A — Ingeniería inversa de la red de fomo.family.

Tres sub-modos (combinables):
  1. HTTP directo al endpoint XHR/fetch que descubras en DevTools (FOMO_A_URL + FOMO_A_HEADERS).
  2. WebSocket interno (FOMO_A_WS_URL + FOMO_A_WS_SUBSCRIBE) si el frontend recibe tokens por push.
  3. Playwright (FOMO_A_PLAYWRIGHT=true): abre la web real en Chromium headless e intercepta las
     respuestas JSON y frames WS cuya URL matchee FOMO_A_PLAYWRIGHT_URL_REGEX. Resuelve cookies,
     Cloudflare (cf_clearance) y tokens efímeros sin que tengas que copiarlos a mano.

Ver docs/FOMO_NETWORK_INSPECTION.md para encontrar los valores exactos.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, AsyncIterator

from ..config import Settings
from ..models import TokenInfo
from .base import JsonHttpTokenSource

log = logging.getLogger(__name__)

BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/129.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://fomo.family",
    "Referer": "https://fomo.family/",
}


class FomoNetworkSource(JsonHttpTokenSource):
    def __init__(self, s: Settings) -> None:
        super().__init__(
            name="fomo:A",
            url=s.fomo_a_url,
            method=s.fomo_a_method,
            headers={**BROWSER_HEADERS, **s.fomo_a_headers},
            body=s.fomo_a_body,
            items_path=s.fomo_a_items_path,
            field_map=s.fomo_a_field_map,
            default_chain=s.fomo_a_default_chain,
            url_template=s.fomo_token_url_template,
            on_fomo=True,
        )
        self.s = s

    async def poll(self, client) -> list[TokenInfo]:  # type: ignore[override]
        if not self.url:  # solo WS / Playwright
            return []
        return await super().poll(client)

    def stream(self) -> AsyncIterator[TokenInfo] | None:
        if self.s.fomo_a_playwright:
            return self._playwright_stream()
        if self.s.fomo_a_ws_url:
            return self._ws_stream()
        return None

    # ---------- WebSocket interno ----------
    async def _ws_stream(self) -> AsyncIterator[TokenInfo]:
        import websockets

        backoff = 1.0
        while True:
            try:
                async with websockets.connect(self.s.fomo_a_ws_url, additional_headers=self.headers,
                                              ping_interval=20, max_size=2**23) as ws:
                    log.info("Vía A: WS conectado %s", self.s.fomo_a_ws_url)
                    backoff = 1.0
                    for msg in self.s.fomo_a_ws_subscribe:
                        await ws.send(msg if isinstance(msg, str) else json.dumps(msg))
                    async for raw in ws:
                        for tok in self._parse_frame(raw):
                            yield tok
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("Vía A WS caído (%s); reintento en %.0fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    def _parse_frame(self, raw: str | bytes) -> list[TokenInfo]:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "ignore")
        # socket.io / phoenix suelen prefijar el JSON ("42[...]"): nos quedamos con el primer [ o {
        m = re.search(r"[\[{]", raw)
        if not m:
            return []
        try:
            data: Any = json.loads(raw[m.start():])
        except json.JSONDecodeError:
            return []
        if isinstance(data, dict) and (data.get("symbol") or data.get("ticker")):
            data = [data]
        return self.parse_payload(data)

    # ---------- Playwright (stealth-ish) ----------
    async def _playwright_stream(self) -> AsyncIterator[TokenInfo]:
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            log.error("FOMO_A_PLAYWRIGHT=true pero falta playwright: pip install playwright && playwright install chromium")
            return
        queue: asyncio.Queue[TokenInfo] = asyncio.Queue()
        url_re = re.compile(self.s.fomo_a_playwright_url_regex, re.I)

        async def on_response(resp) -> None:
            if not url_re.search(resp.url) or "json" not in (resp.headers.get("content-type") or ""):
                return
            try:
                data = await resp.json()
            except Exception:  # noqa: BLE001
                return
            for tok in self.parse_payload(data):
                queue.put_nowait(tok)

        def on_ws(ws) -> None:
            if not url_re.search(ws.url):
                return
            log.info("Vía A/Playwright: WS detectado %s", ws.url)
            ws.on("framereceived", lambda payload: [queue.put_nowait(t) for t in self._parse_frame(payload)])

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True, executable_path=self.s.chromium_path or None,
                                               args=["--disable-blink-features=AutomationControlled"])
            ctx = await browser.new_context(user_agent=BROWSER_HEADERS["User-Agent"], locale="en-US",
                                            viewport={"width": 1366, "height": 900})
            await ctx.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
            page = await ctx.new_page()
            page.on("response", on_response)
            page.on("websocket", on_ws)
            await page.goto(self.s.fomo_a_playwright_page, wait_until="domcontentloaded", timeout=60_000)
            log.info("Vía A/Playwright: escuchando %s", self.s.fomo_a_playwright_page)
            reload_every = max(self.s.fomo_poll_seconds * 6, 30)
            loop = asyncio.get_running_loop()
            next_reload = loop.time() + reload_every
            try:
                while True:
                    try:
                        yield await asyncio.wait_for(queue.get(), timeout=5)
                    except asyncio.TimeoutError:
                        pass
                    if loop.time() >= next_reload:  # fuerza nuevas peticiones XHR si la página no hace polling
                        await page.reload(wait_until="domcontentloaded")
                        next_reload = loop.time() + reload_every
            finally:
                await browser.close()
