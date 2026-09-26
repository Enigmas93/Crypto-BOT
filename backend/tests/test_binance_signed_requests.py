"""Offline tests for the Phase 10 signed-request infrastructure: HMAC
signing, credential guarding, and response parsing. No network calls - a
live smoke test against real testnet credentials is done separately
(scripts/verify_execution_setup.py), since signing correctness on its own
doesn't prove Binance actually accepts the request.
"""
from __future__ import annotations

import hashlib
import hmac

import pytest

from aegis.providers.binance.models import AlgoOrderResult, OrderResult, PositionRisk, TickerStats
from aegis.providers.binance.rest_client import BinanceFuturesRestClient, BinanceOrderError


def _client(api_key: str = "test-key", api_secret: str = "test-secret") -> BinanceFuturesRestClient:
    return BinanceFuturesRestClient(testnet=True, api_key=api_key, api_secret=api_secret)


def test_sign_injects_timestamp_recv_window_and_signature():
    client = _client()
    signed = client._sign({"symbol": "BTCUSDT", "side": "BUY"})

    assert "timestamp" in signed
    assert signed["recvWindow"] == 10000
    assert "signature" in signed
    assert len(signed["signature"]) == 64  # hex-encoded sha256 digest


def test_sign_preserves_caller_params_unchanged():
    client = _client()
    signed = client._sign({"symbol": "BTCUSDT", "side": "BUY", "quantity": "0.01"})

    assert signed["symbol"] == "BTCUSDT"
    assert signed["side"] == "BUY"
    assert signed["quantity"] == "0.01"


def test_sign_signature_matches_hand_computed_hmac_sha256():
    """Pins the exact algorithm (HMAC-SHA256 over the urlencoded query
    string, secret as key) - guards against a future regression like an
    accidental key/message swap or a switch to the wrong digest."""
    from urllib.parse import urlencode

    client = _client(api_secret="my-secret")
    signed = client._sign({"symbol": "BTCUSDT"})

    signature = signed.pop("signature")
    expected_query = urlencode(signed, doseq=True)
    expected = hmac.new(b"my-secret", expected_query.encode(), hashlib.sha256).hexdigest()
    assert signature == expected


def test_sign_produces_different_signatures_for_different_secrets():
    client_a = _client(api_secret="secret-a")
    client_b = _client(api_secret="secret-b")
    # freeze both at the same instant by signing back-to-back is close enough
    # for this equality-of-shape check - what matters is the two never match
    sig_a = client_a._sign({"symbol": "BTCUSDT"})["signature"]
    sig_b = client_b._sign({"symbol": "BTCUSDT"})["signature"]
    assert sig_a != sig_b


@pytest.mark.asyncio
async def test_signed_get_raises_without_credentials():
    client = BinanceFuturesRestClient(testnet=True)  # no api_key/api_secret
    with pytest.raises(BinanceOrderError):
        await client._signed_get("/fapi/v2/balance")
    await client.aclose()


@pytest.mark.asyncio
async def test_signed_write_raises_without_credentials():
    client = BinanceFuturesRestClient(testnet=True)
    with pytest.raises(BinanceOrderError):
        await client._signed_write("POST", "/fapi/v1/order", {"symbol": "BTCUSDT"})
    await client.aclose()


def test_fmt_never_uses_scientific_notation():
    client = _client()
    assert client._fmt(0.00001) == "0.00001"
    assert client._fmt(1.0) == "1"
    assert client._fmt(0.00001000) == "0.00001"


def test_order_result_parses_a_realistic_payload():
    payload = {
        "orderId": 123456789, "clientOrderId": "aegis-abc123", "symbol": "BTCUSDT",
        "side": "BUY", "type": "MARKET", "status": "FILLED",
        "origQty": "0.010", "executedQty": "0.010", "avgPrice": "65000.50",
        "reduceOnly": False, "closePosition": False, "stopPrice": "0",
        "updateTime": 1700000000000,
    }
    result = OrderResult.from_rest_payload(payload)
    assert result.order_id == 123456789
    assert result.status == "FILLED"
    assert result.executed_qty == pytest.approx(0.01)
    assert result.avg_price == pytest.approx(65000.50)
    assert result.stop_price is None  # "0" means "not a stop order", not a real stop price


def test_order_result_parses_a_real_stop_price():
    payload = {
        "orderId": 2, "clientOrderId": "x", "symbol": "BTCUSDT", "side": "SELL",
        "type": "STOP_MARKET", "status": "NEW", "origQty": "0", "executedQty": "0",
        "avgPrice": "0", "reduceOnly": True, "closePosition": False,
        "stopPrice": "64000.0", "updateTime": 1700000000000,
    }
    result = OrderResult.from_rest_payload(payload)
    assert result.stop_price == pytest.approx(64000.0)


def test_position_risk_parses_a_realistic_payload():
    payload = {
        "symbol": "BTCUSDT", "positionAmt": "0.010", "entryPrice": "65000.0",
        "markPrice": "65200.0", "unRealizedProfit": "2.0", "leverage": "3",
        "liquidationPrice": "43000.0",
    }
    position = PositionRisk.from_rest_payload(payload)
    assert position.position_amt == pytest.approx(0.01)
    assert position.leverage == 3
    assert position.unrealized_pnl == pytest.approx(2.0)


def test_algo_order_result_parses_a_not_yet_triggered_order():
    payload = {
        "algoId": 2146760, "clientAlgoId": "aegis-abc", "algoType": "CONDITIONAL",
        "orderType": "STOP_MARKET", "symbol": "BTCUSDT", "side": "SELL", "quantity": "0.01",
        "algoStatus": "NEW", "triggerPrice": "64000.0", "actualPrice": "0", "actualQty": "0",
        "reduceOnly": True, "closePosition": False, "updateTime": 1700000000000,
    }
    result = AlgoOrderResult.from_rest_payload(payload)
    assert result.order_id == 2146760
    assert result.status == "NEW"
    assert result.avg_price == 0.0
    assert result.executed_qty == 0.0
    assert result.stop_price == pytest.approx(64000.0)


def test_algo_order_result_finished_status_normalizes_to_filled():
    payload = {
        "algoId": 2146760, "clientAlgoId": "aegis-abc", "algoType": "CONDITIONAL",
        "orderType": "TAKE_PROFIT_MARKET", "symbol": "BTCUSDT", "side": "SELL", "quantity": "0.01",
        "algoStatus": "FINISHED", "triggerPrice": "68000.0", "actualPrice": "68005.5", "actualQty": "0.01",
        "reduceOnly": True, "closePosition": False, "updateTime": 1700000005000,
    }
    result = AlgoOrderResult.from_rest_payload(payload)
    assert result.status == "FILLED"  # normalized from Binance's "FINISHED"
    assert result.avg_price == pytest.approx(68005.5)
    assert result.executed_qty == pytest.approx(0.01)
    assert result.update_time_ms == 1700000005000


def test_algo_order_result_canceled_and_expired_pass_through_unmapped():
    for algo_status in ("CANCELED", "EXPIRED"):
        payload = {
            "algoId": 1, "clientAlgoId": "x", "orderType": "STOP_MARKET", "symbol": "BTCUSDT",
            "side": "SELL", "quantity": "0.01", "algoStatus": algo_status, "triggerPrice": "64000.0",
            "actualPrice": "0", "actualQty": "0", "reduceOnly": True, "closePosition": False,
            "updateTime": 1700000000000,
        }
        assert AlgoOrderResult.from_rest_payload(payload).status == algo_status


def test_position_risk_flat_position_has_zero_amt():
    payload = {
        "symbol": "BTCUSDT", "positionAmt": "0", "entryPrice": "0", "markPrice": "65200.0",
        "unRealizedProfit": "0", "leverage": "3", "liquidationPrice": "0",
    }
    position = PositionRisk.from_rest_payload(payload)
    assert position.position_amt == 0.0


def test_ticker_stats_parses_a_realistic_payload():
    payload = {
        "symbol": "SOLUSDT", "priceChangePercent": "0.581", "lastPrice": "117.7700",
        "quoteVolume": "2728014460.3988",
    }
    stats = TickerStats.from_rest_payload(payload)
    assert stats.symbol == "SOLUSDT"
    assert stats.price_change_pct == pytest.approx(0.581)
    assert stats.quote_volume == pytest.approx(2728014460.3988)


@pytest.mark.asyncio
async def test_place_trailing_stop_order_rejects_out_of_range_callback_rate():
    client = _client()
    with pytest.raises(ValueError):
        await client.place_trailing_stop_order("BTCUSDT", "SELL", 0.05, 0.01)  # below 0.1 min
    with pytest.raises(ValueError):
        await client.place_trailing_stop_order("BTCUSDT", "SELL", 15.0, 0.01)  # above 10 max


@pytest.mark.asyncio
async def test_place_trailing_stop_order_rounds_callback_rate_to_one_decimal():
    # Real production bug (2026-09-26): once callback_rate_pct became an
    # ATR-derived float (e.g. 2.7430914930998274) instead of a clean
    # hand-set value, Binance rejected every single one with code -2007
    # "Invalid callBack rate" - every Momentum entry since then had its
    # bracket setup fail and get flattened right after filling. Binance
    # only accepts 1 decimal place for this parameter.
    client = _client()
    captured: dict = {}

    async def _fake_signed_write(method, path, params):
        captured.update(params)
        return {"algoId": 1, "clientAlgoId": "x", "orderType": "TRAILING_STOP_MARKET", "symbol": "BTCUSDT",
                "side": "SELL", "quantity": "0.01", "algoStatus": "WORKING", "triggerPrice": "0",
                "actualPrice": "0", "actualQty": "0", "reduceOnly": True, "closePosition": False,
                "updateTime": 1700000000000}

    client._signed_write = _fake_signed_write
    await client.place_trailing_stop_order("BTCUSDT", "SELL", 2.7430914930998274, 0.01)

    assert captured["callbackRate"] == "2.7"


@pytest.mark.asyncio
async def test_set_leverage_rejects_out_of_range_values():
    client = _client()
    with pytest.raises(ValueError):
        await client.set_leverage("BTCUSDT", 0)
    with pytest.raises(ValueError):
        await client.set_leverage("BTCUSDT", 126)


@pytest.mark.asyncio
async def test_set_leverage_raises_without_credentials():
    client = BinanceFuturesRestClient(testnet=True)  # no api_key/api_secret
    with pytest.raises(BinanceOrderError):
        await client.set_leverage("BTCUSDT", 3)
    await client.aclose()
