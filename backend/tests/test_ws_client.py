import asyncio
import json

import pytest
from websockets.exceptions import ConnectionClosedOK

from aegis.providers.binance import ws_client as ws_client_module
from aegis.providers.binance.ws_client import BinanceFuturesWebSocketClient


class FakeWebSocket:
    """Stands in for a `websockets` connection. `messages` is a list of
    already-encoded JSON strings served in order; once exhausted, either
    raises `close_with` or sleeps forever (to simulate a hung connection so
    the client's staleness timeout has something to trip on)."""

    def __init__(self, messages, close_with: Exception | None = None, hang_after: bool = False):
        self._messages = list(messages)
        self._close_with = close_with
        self._hang_after = hang_after

    async def recv(self):
        if self._messages:
            return self._messages.pop(0)
        if self._hang_after:
            await asyncio.sleep(1000)
        if self._close_with is not None:
            raise self._close_with
        await asyncio.sleep(1000)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _envelope(stream: str, data: dict) -> str:
    return json.dumps({"stream": stream, "data": data})


KLINE_MSG = _envelope("btcusdt@kline_1m", {"e": "kline", "s": "BTCUSDT"})


@pytest.mark.asyncio
async def test_receives_messages_then_stops_cleanly(monkeypatch):
    fake_ws = FakeWebSocket([KLINE_MSG, KLINE_MSG], close_with=ConnectionClosedOK(None, None))

    def fake_connect(url, **kwargs):
        return fake_ws

    monkeypatch.setattr(ws_client_module.websockets, "connect", fake_connect)

    client = BinanceFuturesWebSocketClient(streams=["btcusdt@kline_1m"], testnet=True)
    received = []

    def on_message(data):
        received.append(data)
        if len(received) == 2:
            client.stop()

    await client.run(on_message=on_message)

    assert len(received) == 2
    assert received[0]["e"] == "kline"
    assert client.reconnect_count == 0


@pytest.mark.asyncio
async def test_stale_connection_triggers_reconnect(monkeypatch):
    connect_calls = {"n": 0}

    def fake_connect(url, **kwargs):
        connect_calls["n"] += 1
        return FakeWebSocket([], hang_after=True)

    monkeypatch.setattr(ws_client_module.websockets, "connect", fake_connect)

    client = BinanceFuturesWebSocketClient(
        streams=["btcusdt@kline_1m"], testnet=True, stale_after_seconds=0.02,
    )

    async def on_reconnect():
        client.stop()

    await asyncio.wait_for(client.run(on_message=lambda d: None, on_reconnect=on_reconnect), timeout=5)

    # the stale connection is detected and torn down; `stop()` (called from
    # inside on_reconnect) wins the race before a second `connect()` fires
    assert connect_calls["n"] == 1
    assert client.reconnect_count == 1


@pytest.mark.asyncio
async def test_bad_json_payload_does_not_crash_the_client(monkeypatch):
    fake_ws = FakeWebSocket(["not json", KLINE_MSG], close_with=ConnectionClosedOK(None, None))

    def fake_connect(url, **kwargs):
        return fake_ws

    monkeypatch.setattr(ws_client_module.websockets, "connect", fake_connect)

    client = BinanceFuturesWebSocketClient(streams=["btcusdt@kline_1m"], testnet=True)
    received = []

    def on_message(data):
        received.append(data)
        client.stop()

    await client.run(on_message=on_message)

    assert len(received) == 1
    assert received[0]["e"] == "kline"
