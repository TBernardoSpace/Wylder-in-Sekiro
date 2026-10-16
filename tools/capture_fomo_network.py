"""Descubre automáticamente los endpoints internos de fomo.family (apoyo a la Vía A).

Abre la web con Chromium (visible por defecto, para que puedas pasar Cloudflare / login si hace falta),
registra todas las respuestas JSON y los frames de WebSocket, y al terminar imprime un ranking de
endpoints que parecen listas de tokens, con la configuración .env sugerida para cada uno.

Uso:
    pip install playwright && playwright install chromium
    python tools/capture_fomo_network.py --url https://fomo.family/ --seconds 60
    python tools/capture_fomo_network.py --headless --out captura.json

Navega tú por la sección de tokens nuevos / trending / Memes mientras corre.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from radar.fomo.base import DEFAULT_FIELD_CANDIDATES  # noqa: E402
from radar.utils import find_item_list, first_path  # noqa: E402

INTERESTING_HEADERS = ("authorization", "x-api-key", "x-client", "x-app", "cookie", "x-requested-with", "apikey")


def score_items(items: list[dict]) -> tuple[int, list[str]]:
    """Cuántos de los campos clave (symbol, address, liquidez...) aparecen en el primer item."""
    if not items:
        return 0, []
    sample = items[0]
    hits = [f for f, paths in DEFAULT_FIELD_CANDIDATES.items() if first_path(sample, paths) is not None]
    return len(hits), hits


def items_path_of(data, items) -> str:
    """Busca la ruta con puntos hasta la lista detectada (para FOMO_A_ITEMS_PATH)."""
    def walk(node, path):
        if node is items:
            return path
        if isinstance(node, dict):
            for k, v in node.items():
                r = walk(v, f"{path}.{k}" if path else k)
                if r is not None:
                    return r
        return None
    return walk(data, "") or ""


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="https://fomo.family/")
    ap.add_argument("--seconds", type=int, default=60)
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--out", default="fomo_capture.json")
    ap.add_argument("--chromium", default=os.environ.get("CHROMIUM_PATH") or None,
                    help="ruta a un Chromium ya instalado (si no usas `playwright install`)")
    args = ap.parse_args()

    from playwright.async_api import async_playwright

    captured: list[dict] = []
    ws_frames: list[dict] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=args.headless, executable_path=args.chromium,
                                           args=["--disable-blink-features=AutomationControlled"])
        ctx = await browser.new_context(locale="en-US", viewport={"width": 1366, "height": 900})
        page = await ctx.new_page()

        async def on_response(resp):
            ctype = resp.headers.get("content-type") or ""
            if "json" not in ctype:
                return
            try:
                data = await resp.json()
            except Exception:  # noqa: BLE001
                return
            req = resp.request
            items = find_item_list(data)
            n_hits, hits = score_items(items)
            req_headers = await req.all_headers()
            captured.append({
                "url": resp.url, "method": req.method, "status": resp.status,
                "post_data": req.post_data, "n_items": len(items), "field_hits": hits, "score": n_hits,
                "items_path": items_path_of(data, items) if items else "",
                "headers": {k: v for k, v in req_headers.items() if k.lower() in INTERESTING_HEADERS},
                "sample": items[0] if items else data if isinstance(data, dict) else None,
            })
            if n_hits >= 3:
                print(f"[XHR] {req.method} {resp.url}  items={len(items)}  campos={hits}")

        def on_ws(ws):
            print(f"[WS ] abierto {ws.url}")

            def frame(direction):
                def _h(payload):
                    if len(ws_frames) < 400:
                        ws_frames.append({"url": ws.url, "dir": direction, "payload": str(payload)[:2000]})
                return _h
            ws.on("framesent", frame("sent"))
            ws.on("framereceived", frame("recv"))

        page.on("response", on_response)
        page.on("websocket", on_ws)
        await page.goto(args.url, wait_until="domcontentloaded", timeout=90_000)
        print(f"Capturando {args.seconds}s… navega por la sección de tokens nuevos / trending / Memes.")
        await asyncio.sleep(args.seconds)
        await browser.close()

    best = sorted(captured, key=lambda c: (c["score"], c["n_items"]), reverse=True)
    Path(args.out).write_text(json.dumps({"responses": best, "ws_frames": ws_frames}, indent=2, default=str))
    print(f"\nGuardado en {args.out}\n\n=== Candidatos (mejor primero) ===")
    for c in best[:5]:
        if c["score"] < 2:
            continue
        parts = urlsplit(c["url"])
        print(f"\n# score={c['score']} items={c['n_items']} {c['method']} {parts.netloc}{parts.path}")
        print(f"FOMO_A_URL={c['url']}")
        print(f"FOMO_A_METHOD={c['method']}")
        if c["post_data"]:
            print(f"FOMO_A_BODY={c['post_data']}")
        if c["items_path"]:
            print(f"FOMO_A_ITEMS_PATH={c['items_path']}")
        if c["headers"]:
            print(f"FOMO_A_HEADERS={json.dumps(c['headers'])}")
    ws_urls = sorted({f['url'] for f in ws_frames})
    if ws_urls:
        print("\n=== WebSockets vistos ===")
        for u in ws_urls:
            sent = [f["payload"] for f in ws_frames if f["url"] == u and f["dir"] == "sent"][:3]
            print(f"FOMO_A_WS_URL={u}\n# primeros mensajes enviados (candidatos a FOMO_A_WS_SUBSCRIBE): {sent}")


if __name__ == "__main__":
    asyncio.run(main())
