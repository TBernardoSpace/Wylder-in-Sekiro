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
    args = parser.parse_args()

    if args.demo:
        os.environ["DEMO_MODE"] = "true"
    settings = Settings.from_env(args.env_file)
    if args.demo:
        settings.db_path = ":memory:"
    logging.basicConfig(level=settings.log_level.upper(),
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    uvicorn.run(create_app(settings), host=args.host or settings.host, port=args.port or settings.port,
                log_level="warning")


if __name__ == "__main__":
    main()
