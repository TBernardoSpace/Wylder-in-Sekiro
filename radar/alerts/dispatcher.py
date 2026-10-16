"""Envío de alertas a Telegram y Discord (en paralelo, con reintento ante rate-limit)."""
from __future__ import annotations

import asyncio
import logging

import httpx

from ..config import Settings
from ..models import Signal
from .formatter import discord_payload, telegram_keyboard, telegram_text

log = logging.getLogger(__name__)


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str, client: httpx.AsyncClient) -> None:
        self.url = f"https://api.telegram.org/bot{token}/sendMessage"
        self.chat_id, self.client = chat_id, client

    async def send(self, sig: Signal, new_leader: bool = False) -> None:
        body = {
            "chat_id": self.chat_id,
            "text": telegram_text(sig, new_leader),
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
            "disable_notification": False,  # notificación CON sonido
            "reply_markup": telegram_keyboard(sig),
        }
        for attempt in range(3):
            r = await self.client.post(self.url, json=body)
            if r.status_code == 429:
                wait = (r.json().get("parameters") or {}).get("retry_after", 2)
                await asyncio.sleep(float(wait))
                continue
            if r.status_code == 400 and "copy_text" in r.text and attempt == 0:
                # Bot API antigua (self-hosted) sin copy_text: reintenta sin ese botón
                body["reply_markup"]["inline_keyboard"] = [
                    [b for b in row if "copy_text" not in b] for row in body["reply_markup"]["inline_keyboard"]
                ]
                continue
            if r.is_error:
                log.error("Telegram %s: %s", r.status_code, r.text[:300])
            return


class DiscordNotifier:
    def __init__(self, webhook_url: str, mention: str, client: httpx.AsyncClient) -> None:
        self.url, self.mention, self.client = webhook_url, mention, client

    async def send(self, sig: Signal, new_leader: bool = False) -> None:
        for _ in range(3):
            r = await self.client.post(self.url, json=discord_payload(sig, self.mention, new_leader))
            if r.status_code == 429:
                await asyncio.sleep(float(r.json().get("retry_after", 1)))
                continue
            if r.is_error:
                log.error("Discord %s: %s", r.status_code, r.text[:300])
            return


class AlertDispatcher:
    def __init__(self, settings: Settings) -> None:
        self.client = httpx.AsyncClient(timeout=10)
        self.targets: list[TelegramNotifier | DiscordNotifier] = []
        if settings.telegram_bot_token and settings.telegram_chat_id:
            self.targets.append(TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id, self.client))
        if settings.discord_webhook_url:
            self.targets.append(DiscordNotifier(settings.discord_webhook_url, settings.discord_mention, self.client))
        if not self.targets:
            log.warning("Sin Telegram/Discord configurados: las señales solo se verán en el dashboard")

    async def send(self, signal: Signal, *, new_leader: bool = False) -> None:
        results = await asyncio.gather(*(t.send(signal, new_leader) for t in self.targets), return_exceptions=True)
        for t, res in zip(self.targets, results):
            if isinstance(res, Exception):
                log.error("Fallo enviando alerta por %s: %s", type(t).__name__, res)

    async def close(self) -> None:
        await self.client.aclose()
