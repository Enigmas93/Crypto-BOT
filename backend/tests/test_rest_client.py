import httpx
import pytest

from aegis.providers.binance.rest_client import BinanceFuturesRestClient, BinanceRestError

EXCHANGE_INFO = {
    "symbols": [
        {
            "symbol": "BTCUSDT",
            "status": "TRADING",
            "pricePrecision": 2,
            "quantityPrecision": 3,
            "filters": [
                {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
                {"filterType": "LOT_SIZE", "stepSize": "0.001"},
                {"filterType": "MIN_NOTIONAL", "notional": "5.0"},
            ],
        }
    ]
}


def _client_with_transport(handler) -> BinanceFuturesRestClient:
    client = BinanceFuturesRestClient(testnet=True)
    client._client = httpx.AsyncClient(
        base_url=client._client.base_url,
        transport=httpx.MockTransport(handler),
    )
    return client


@pytest.mark.asyncio
async def test_get_symbol_rules_parses_filters():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/fapi/v1/exchangeInfo"
        return httpx.Response(200, json=EXCHANGE_INFO)

    client = _client_with_transport(handler)
    rules = await client.get_symbol_rules()
    assert rules["BTCUSDT"].tick_size == 0.10
    assert rules["BTCUSDT"].step_size == 0.001
    assert rules["BTCUSDT"].min_notional == 5.0
    await client.aclose()


@pytest.mark.asyncio
async def test_get_klines_parses_rows():
    row = [1499040000000, "1", "2", "0.5", "1.5", "100", 1499644799999,
           "150", 10, "60", "90", "0"]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["symbol"] == "BTCUSDT"
        return httpx.Response(200, json=[row])

    client = _client_with_transport(handler)
    klines = await client.get_klines("BTCUSDT", "1m", limit=1)
    assert len(klines) == 1
    assert klines[0].close == 1.5
    await client.aclose()


@pytest.mark.asyncio
async def test_non_retryable_4xx_raises_immediately():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400, text="bad symbol")

    client = _client_with_transport(handler)
    with pytest.raises(BinanceRestError):
        await client.get_klines("NOPE", "1m")
    assert calls["n"] == 1  # no retries on a non-retryable 4xx
    await client.aclose()


@pytest.mark.asyncio
async def test_retryable_error_then_success():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503, text="unavailable")
        return httpx.Response(200, json=EXCHANGE_INFO)

    client = _client_with_transport(handler)
    result = await client.get_exchange_info()
    assert result == EXCHANGE_INFO
    assert calls["n"] == 3
    await client.aclose()
