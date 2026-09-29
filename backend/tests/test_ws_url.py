"""Binance split production WS paths (found 2026-09-29): market streams on
/market/stream, book streams on /public/stream. The legacy /stream accepted
kline subscriptions but sent nothing, silently starving the collector."""
import pytest

from aegis.providers.binance.ws_client import BinanceFuturesWebSocketClient


def test_market_streams_use_the_market_path():
    ws = BinanceFuturesWebSocketClient(["btcusdt@kline_1m", "btcusdt@markPrice@1s"], testnet=False)
    assert ws.url == "wss://fstream.binance.com/market/stream?streams=btcusdt@kline_1m/btcusdt@markPrice@1s"


def test_liquidation_stream_uses_the_market_path():
    assert BinanceFuturesWebSocketClient(["!forceOrder@arr"], testnet=False).url.startswith(
        "wss://fstream.binance.com/market/stream")


def test_depth_streams_use_the_public_path():
    ws = BinanceFuturesWebSocketClient(["btcusdt@depth20@100ms"], testnet=False, channel="public")
    assert ws.url.startswith("wss://fstream.binance.com/public/stream?streams=")


def test_testnet_keeps_its_single_path():
    assert BinanceFuturesWebSocketClient(["btcusdt@kline_1m"], testnet=True).url.startswith(
        "wss://stream.binancefuture.com/stream")


def test_unknown_channel_rejected():
    with pytest.raises(ValueError):
        BinanceFuturesWebSocketClient(["x"], testnet=False, channel="private")
