import httpx
import pytest

from aegis.providers.bingx.rest_client import (
    BingXFuturesRestClient,
    BingXOrderError,
    BingXRestError,
    from_bingx_symbol,
    to_bingx_symbol,
)

CONTRACTS = [
    {
        "symbol": "BTC-USDT", "status": "1", "pricePrecision": 1, "quantityPrecision": 3,
        "tradeMinQuantity": 0.0001, "tradeMinUSDT": 2, "maxLongLeverage": 125, "maxShortLeverage": 125,
    },
]

_SERVER_TIME_PATH = "/openApi/swap/v2/server/time"


def test_to_bingx_symbol_inserts_hyphen_before_usdt():
    assert to_bingx_symbol("BTCUSDT") == "BTC-USDT"
    assert to_bingx_symbol("1000PEPEUSDT") == "1000PEPE-USDT"


def test_from_bingx_symbol_strips_hyphen():
    assert from_bingx_symbol("BTC-USDT") == "BTCUSDT"
    assert from_bingx_symbol("1000PEPE-USDT") == "1000PEPEUSDT"


def _client_with_transport(handler) -> BingXFuturesRestClient:
    client = BingXFuturesRestClient(testnet=True, api_key="test-key", api_secret="test-secret")
    client._client = httpx.AsyncClient(base_url=client._client.base_url, transport=httpx.MockTransport(handler))
    return client


def _ok(data):
    return httpx.Response(200, json={"code": 0, "msg": "", "data": data})


@pytest.mark.asyncio
async def test_get_symbol_rules_parses_contracts_and_maps_symbol_back():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/openApi/swap/v2/quote/contracts"
        return _ok(CONTRACTS)

    client = _client_with_transport(handler)
    rules = await client.get_symbol_rules()
    assert rules["BTCUSDT"].tick_size == pytest.approx(0.1)
    assert rules["BTCUSDT"].step_size == pytest.approx(0.001)
    assert rules["BTCUSDT"].min_notional == 2.0
    await client.aclose()


@pytest.mark.asyncio
async def test_non_retryable_error_code_raises_immediately():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"code": 109425, "msg": "symbol not supported", "data": {}})

    client = _client_with_transport(handler)
    with pytest.raises(BingXRestError):
        await client.get_contracts()
    assert calls["n"] == 1  # no retries on a non-retryable error code
    await client.aclose()


@pytest.mark.asyncio
async def test_retryable_error_code_then_success():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(200, json={"code": 100410, "msg": "rate limited", "data": {}})
        return _ok(CONTRACTS)

    client = _client_with_transport(handler)
    result = await client.get_contracts()
    assert result == CONTRACTS
    assert calls["n"] == 3
    await client.aclose()


def _handler_with_clock_sync(signed_handler):
    """Wraps a handler that only cares about the signed call under test,
    transparently answering the clock-sync GET every signed call triggers."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == _SERVER_TIME_PATH:
            return _ok({"serverTime": 1700000000000})
        return signed_handler(request)

    return handler


@pytest.mark.asyncio
async def test_signed_get_includes_api_key_and_source_key_headers():
    seen = {}

    def signed_handler(request: httpx.Request) -> httpx.Response:
        seen["api_key"] = request.headers.get("X-BX-APIKEY")
        seen["source_key"] = request.headers.get("X-SOURCE-KEY")
        seen["signature_present"] = "signature" in request.url.params
        seen["timestamp_present"] = "timestamp" in request.url.params
        return _ok([])

    client = _client_with_transport(_handler_with_clock_sync(signed_handler))
    await client.get_account_balance()
    assert seen["api_key"] == "test-key"
    assert seen["source_key"] == "BX-AI-SKILL"
    assert seen["signature_present"] is True
    assert seen["timestamp_present"] is True
    await client.aclose()


@pytest.mark.asyncio
async def test_signed_get_without_credentials_raises_before_any_request():
    client = BingXFuturesRestClient(testnet=True)  # no api_key/api_secret
    with pytest.raises(BingXOrderError):
        await client.get_account_balance()
    await client.aclose()


@pytest.mark.asyncio
async def test_place_market_order_sends_json_body_with_one_way_position_side():
    seen = {}

    def signed_handler(request: httpx.Request) -> httpx.Response:
        import json
        body = json.loads(request.content)
        seen["body"] = body
        return _ok({
            "orderId": 123456789, "clientOrderId": body["clientOrderId"], "symbol": "BTC-USDT",
            "side": "BUY", "type": "MARKET", "status": "FILLED", "origQty": "0.01",
            "executedQty": "0.01", "avgPrice": "65000.0",
        })

    client = _client_with_transport(_handler_with_clock_sync(signed_handler))
    order = await client.place_market_order("BTCUSDT", "BUY", 0.01)
    assert seen["body"]["symbol"] == "BTC-USDT"
    assert seen["body"]["positionSide"] == "BOTH"
    assert "signature" in seen["body"]
    assert order.order_id == 123456789
    assert order.symbol == "BTCUSDT"  # normalized back to canonical form
    assert order.avg_price == 65000.0
    await client.aclose()


@pytest.mark.asyncio
async def test_place_trailing_stop_order_converts_percent_to_bingx_fraction():
    seen = {}

    def signed_handler(request: httpx.Request) -> httpx.Response:
        import json
        body = json.loads(request.content)
        seen["body"] = body
        return _ok({
            "orderId": 999, "clientOrderId": body["clientOrderId"], "symbol": "BTC-USDT",
            "side": "SELL", "type": "TRAILING_STOP_MARKET", "status": "NEW", "origQty": "0.01",
        })

    client = _client_with_transport(_handler_with_clock_sync(signed_handler))
    await client.place_trailing_stop_order("BTCUSDT", "SELL", 2.0, 0.01, activation_price=66000.0)
    assert seen["body"]["priceRate"] == pytest.approx(0.02)  # 2% -> 0.02 fraction, not "2.0"
    assert seen["body"]["activationPrice"] == 66000.0
    assert seen["body"]["type"] == "TRAILING_STOP_MARKET"
    await client.aclose()


@pytest.mark.asyncio
async def test_place_trailing_stop_order_rejects_out_of_range_callback_rate():
    client = BingXFuturesRestClient(testnet=True, api_key="k", api_secret="s")
    with pytest.raises(ValueError):
        await client.place_trailing_stop_order("BTCUSDT", "SELL", 20.0, 0.01)
    await client.aclose()


@pytest.mark.asyncio
async def test_place_market_order_never_retries_on_transport_error():
    def signed_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    client = _client_with_transport(_handler_with_clock_sync(signed_handler))
    with pytest.raises(BingXOrderError):
        await client.place_market_order("BTCUSDT", "BUY", 0.01)
    await client.aclose()


@pytest.mark.asyncio
async def test_cancel_order_swallows_nothing_but_raises_with_code():
    def signed_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 109421, "msg": "order does not exist", "data": {}})

    client = _client_with_transport(_handler_with_clock_sync(signed_handler))
    with pytest.raises(BingXOrderError) as exc_info:
        await client.cancel_order("BTCUSDT", 1)
    assert exc_info.value.code == 109421
    await client.aclose()


@pytest.mark.asyncio
async def test_set_leverage_rejects_out_of_range_values():
    client = BingXFuturesRestClient(testnet=True, api_key="k", api_secret="s")
    with pytest.raises(ValueError):
        await client.set_leverage("BTCUSDT", 200)
    await client.aclose()


@pytest.mark.asyncio
async def test_cancel_all_after_rejects_out_of_range_timeout():
    client = BingXFuturesRestClient(testnet=True, api_key="k", api_secret="s")
    with pytest.raises(ValueError):
        await client.cancel_all_after(5)
    with pytest.raises(ValueError):
        await client.cancel_all_after(200)
    await client.aclose()


@pytest.mark.asyncio
async def test_get_position_risk_converts_unsigned_amount_and_side_to_signed():
    def signed_handler(request: httpx.Request) -> httpx.Response:
        return _ok([
            {"symbol": "BTC-USDT", "positionAmt": "0.01", "positionSide": "SHORT", "avgPrice": "65000",
             "markPrice": "64000", "unrealizedProfit": "10", "leverage": 3, "liquidationPrice": "70000"},
        ])

    client = _client_with_transport(_handler_with_clock_sync(signed_handler))
    positions = await client.get_position_risk("BTCUSDT")
    assert positions[0].position_amt == -0.01  # SHORT -> negative, matching Binance's convention
    assert positions[0].symbol == "BTCUSDT"
    await client.aclose()


@pytest.mark.asyncio
async def test_get_position_mode_parses_string_false_as_one_way_not_truthy():
    """Regression: BingX returns dualSidePosition as the STRING "false",
    not a JSON boolean - a naive bool(...) on that string is True (any
    non-empty Python string is truthy), which would make one-way mode
    look like Hedge mode forever. Caught live 2026-09-24."""

    def signed_handler(request: httpx.Request) -> httpx.Response:
        return _ok({"dualSidePosition": "false"})

    client = _client_with_transport(_handler_with_clock_sync(signed_handler))
    assert await client.get_position_mode() is False
    await client.aclose()


@pytest.mark.asyncio
async def test_get_position_mode_parses_string_true_as_hedge_mode():
    def signed_handler(request: httpx.Request) -> httpx.Response:
        return _ok({"dualSidePosition": "true"})

    client = _client_with_transport(_handler_with_clock_sync(signed_handler))
    assert await client.get_position_mode() is True
    await client.aclose()


@pytest.mark.asyncio
async def test_apply_vst_returns_updated_balance():
    def signed_handler(request: httpx.Request) -> httpx.Response:
        import json
        body = json.loads(request.content)
        assert body["adjustType"] == 0
        assert body["amount"] == "1000"
        return _ok({"balance": "1000.0383"})

    client = _client_with_transport(_handler_with_clock_sync(signed_handler))
    balance = await client.apply_vst(1000, increase=True)
    assert balance == "1000.0383"
    await client.aclose()
