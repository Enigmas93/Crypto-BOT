"""Telegram alerting (Fase 15b) - the NotificationService the original
blueprint called for, scoped to what actually matters operationally:
Kill Switch triggers and situations that need a human right now (a bracket
leg failing AND the emergency flatten also failing, leaving a real
position unprotected).

Best-effort by construction: a notification failing to send must never
raise into, block, or crash the real trading logic that was trying to
report something - the only thing worse than not being alerted is an
alert-delivery bug taking down the engine that needed to alert. Every
failure path here logs and returns, never raises.

Verified live 2026-09-23 against the real Telegram Bot API
(`POST https://api.telegram.org/bot<token>/sendMessage`) with the user's
own bot token and channel id before being wired into anything.
"""
from __future__ import annotations

import httpx

from aegis.logging_utils import get_logger, log_event

_LOG = get_logger("notifications.telegram")
_API_BASE = "https://api.telegram.org"


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str, timeout: float = 10.0) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id
        self._client = httpx.AsyncClient(timeout=timeout)

    @property
    def enabled(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def send(self, message: str) -> None:
        """Fire-and-forget - never raises. Silently does nothing if not
        configured (TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID unset), same
        "optional, skip if missing" policy as every other external
        integration in this project."""
        if not self.enabled:
            return
        try:
            response = await self._client.post(
                f"{_API_BASE}/bot{self.bot_token}/sendMessage",
                json={"chat_id": self.chat_id, "text": message},
            )
            if response.status_code != 200:
                # Never log the token or chat_id - the URL/payload could
                # end up in a log aggregator; the token is a bearer
                # credential (spec's "secrets never in logs" rule).
                log_event(_LOG, "telegram_send_failed", level=30, status=response.status_code)
        except httpx.TransportError as exc:
            log_event(_LOG, "telegram_transport_error", level=30, error=str(exc))
