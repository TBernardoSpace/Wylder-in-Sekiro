"""Punto de entrada: `python -m radar [--demo] [--port 8000]`."""
from __future__ import annotations

import argparse
import logging
import os

import uvicorn

from .api.server import create_app
from .config import Settings


def main() -> None:
    parser = argparse.ArgumentParser(description="News-to-Token Signal Radar (fomo.family)")
    parser.add_argument("--demo", action="store_true", help="simulación offline sin claves")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--replay", default=None, help="escenario JSON a reproducir (ver scenarios/)")
    parser.add_argument("--speed", type=float, default=None, help="minutos de escenario por minuto real")
    parser.add_argument("--headless", action="store_true", help="con --replay: solo reporte, sin dashboard")
    args = parser.parse_args()

    if args.demo:
        os.environ["DEMO_MODE"] = "true"
    settings = Settings.from_env(args.env_file)
    if args.demo:
        settings.db_path = ":memory:"
    logging.basicConfig(level=settings.log_level.upper(),
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if args.replay:
        from .replay import Scenario, run_headless, scale_settings

        speed = args.speed or Scenario.load(args.replay).data.get("speed", 30)
        if args.headless:
            import asyncio
            import sys

            logging.getLogger().setLevel(logging.ERROR)
            report, ok = asyncio.run(run_headless(settings, args.replay, speed))
            print(report)
            sys.exit(0 if ok else 1)
        settings.replay_path, settings.replay_speed = args.replay, speed
        scale_settings(settings, speed)
    uvicorn.run(create_app(settings), host=args.host or settings.host, port=args.port or settings.port,
                log_level="warning")


if __name__ == "__main__":
    main()
