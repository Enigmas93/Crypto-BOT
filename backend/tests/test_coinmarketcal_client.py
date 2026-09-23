import httpx
import pytest

from aegis.providers.coinmarketcal.client import CoinMarketCalProvider, CoinMarketCalProviderError

# A real event payload, captured live against api.coinmarketcal.com 2026-09-23
# (see aegis/providers/coinmarketcal/client.py's module docstring).
EVENTS_PAYLOAD = {
    "data": [
        {
            "id": "98629", "slug": "glamsterdam-testnets-59318741-1", "title": "Glamsterdam testnets",
            "description": None, "date": "2026-09-30T00:00:00Z", "dateEnd": "", "dateType": "month",
            "isEstimated": True, "displayedDate": "Sep 2026",
            "coins": [{"slug": "ethereum", "symbol": "eth", "name": "Ethereum"}],
            "impact": None, "impactSummary": None, "sourceUrl": None, "snapshotUrl": None,
            "lastVerifiedAt": None, "createdAt": "2026-09-16T13:00:53Z", "updatedAt": "2026-09-20T08:45:53Z",
        },
    ],
    "meta": {"total": 1, "limit": 5, "cursor": None},
}


def _provider_with_transport(handler, api_key: str = "test-key") -> CoinMarketCalProvider:
    provider = CoinMarketCalProvider(api_key=api_key)
    provider._client = httpx.AsyncClient(
        base_url=provider._client.base_url, transport=httpx.MockTransport(handler)
    )
    return provider


@pytest.mark.asyncio
async def test_get_events_returns_raw_event_dicts_and_sends_the_right_auth_header():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-api-key"] == "test-key"
        assert request.url.params["coins"] == "bitcoin,ethereum"
        return httpx.Response(200, json=EVENTS_PAYLOAD)

    provider = _provider_with_transport(handler)
    events = await provider.get_events(["bitcoin", "ethereum"])

    assert len(events) == 1
    assert events[0]["title"] == "Glamsterdam testnets"
    await provider.aclose()


@pytest.mark.asyncio
async def test_get_events_without_api_key_raises_immediately():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=EVENTS_PAYLOAD)

    provider = _provider_with_transport(handler, api_key="")
    with pytest.raises(CoinMarketCalProviderError):
        await provider.get_events(["bitcoin"])
    assert calls["n"] == 0
    await provider.aclose()


@pytest.mark.asyncio
async def test_non_retryable_error_raises_immediately():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(403, json={"message": "Forbidden"})

    provider = _provider_with_transport(handler)
    with pytest.raises(CoinMarketCalProviderError):
        await provider.get_events(["bitcoin"])
    assert calls["n"] == 1
    await provider.aclose()


@pytest.mark.asyncio
async def test_retryable_error_then_success():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503, text="unavailable")
        return httpx.Response(200, json=EVENTS_PAYLOAD)

    provider = _provider_with_transport(handler)
    events = await provider.get_events(["bitcoin"])
    assert len(events) == 1
    assert calls["n"] == 3
    await provider.aclose()
