"""Binance Futures combined WebSocket client.

Implements spec section 6 in full: persistent connection, reconnection with
exponential backoff, staleness detection (no message != connection dropped
silently), proactive renewal before Binance's own connection-age cap, and a
hook so the caller (MarketCollector) can reconcile state via REST after
every reconnect. A WebSocket connection is never assumed to stay open
forever.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import time
from collections.abc import Awaitable, Callable
from typing import Any

import websockets
from websockets.exceptions import ConnectionClosed

from aegis.logging_utils import get_logger, log_event
from aegis.providers.binance.constants import (
    WS_BASE_URL_PROD,
    WS_BASE_URL_TESTNET,
    WS_MAX_CONNECTION_SECONDS,
    WS_STALE_AFTER_SECONDS,
)
from aegis.utils.backoff import BackoffPolicy

_LOG = get_logger("binance.ws")

OnMessage = Callable[[dict[str, Any]], None]
OnReconnect = Callable[[], Awaitable[None] | None]


class BinanceFuturesWebSocketClient:
    def __init__(
        self,
        streams: list[str],
        testnet: bool = True,
        stale_after_seconds: float = WS_STALE_AFTER_SECONDS,
        max_connection_seconds: float = WS_MAX_CONNECTION_SECONDS,
    ) -> None:
        if not streams:
            raise ValueError("streams must be a non-empty list")
        self.streams = streams
        self.testnet = testnet
        self.stale_after_seconds = stale_after_seconds
        self.max_connection_seconds = max_connection_seconds
        self._stop_event = asyncio.Event()
        self.last_message_at: float | None = None
        self.messages_received = 0
        self.reconnect_count = 0

    @property
    def url(self) -> str:
        base = WS_BASE_URL_TESTNET if self.testnet else WS_BASE_URL_PROD
        return f"{base}?streams={'/'.join(self.streams)}"

    def stop(self) -> None:
        self._stop_event.set()

    async def run(self, on_message: OnMessage, on_reconnect: OnReconnect | None = None) -> None:
        """Run forever (until `stop()`), reconnecting as needed."""
        backoff = BackoffPolicy(base_seconds=1.0, max_seconds=60.0)
        first_connection = True

        while not self._stop_event.is_set():
            try:
                await self._run_one_connection(on_message, backoff, first_connection)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - reconnect on anything, log everything
                delay = backoff.next_delay()
                log_event(
                    _LOG, "ws_connection_error", level=40,
                    error=str(exc), attempt=backoff.attempt, retry_in_seconds=round(delay, 2),
                )
                if self._stop_event.is_set():
                    break
                await asyncio.sleep(delay)
                continue

            if self._stop_event.is_set():
                break

            self.reconnect_count += 1
            log_event(_LOG, "ws_reconnecting", reconnect_count=self.reconnect_count)
            if on_reconnect is not None:
                result = on_reconnect()
                if inspect.isawaitable(result):
                    await result
            first_connection = False

    async def _run_one_connection(
        self, on_message: OnMessage, backoff: BackoffPolicy, first_connection: bool
    ) -> None:
        connected_at = time.monotonic()
        self.last_message_at = None

        async with websockets.connect(self.url, ping_interval=None, close_timeout=5) as ws:
            log_event(_LOG, "ws_connected", url_streams=len(self.streams))
            backoff.reset()

            while not self._stop_event.is_set():
                remaining_life = self.max_connection_seconds - (time.monotonic() - connected_at)
                if remaining_life <= 0:
                    log_event(_LOG, "ws_proactive_renew")
                    return  # loop in run() will reconnect

                timeout = min(self.stale_after_seconds, max(remaining_life, 0.1))
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                except TimeoutError:
                    since = time.monotonic() - (self.last_message_at or connected_at)
                    log_event(_LOG, "ws_stale_no_data", level=30, seconds_idle=round(since, 1))
                    return  # force reconnect - never assume the socket is still alive
                except ConnectionClosed as exc:
                    log_event(_LOG, "ws_connection_closed", level=30, code=exc.code, reason=str(exc.reason))
                    return

                self.last_message_at = time.monotonic()
                self.messages_received += 1
                self._dispatch(raw, on_message)

    def _dispatch(self, raw: str | bytes, on_message: OnMessage) -> None:
        try:
            envelope = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            log_event(_LOG, "ws_bad_payload", level=30, raw_preview=str(raw)[:200])
            return

        data = envelope.get("data", envelope)
        try:
            on_message(data)
        except Exception as exc:  # noqa: BLE001 - one bad handler must not kill the stream
            log_event(_LOG, "ws_handler_error", level=40, error=str(exc))
