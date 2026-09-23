import httpx
import pytest

from aegis.providers.fred.client import FredProvider, FredProviderError

OBSERVATIONS_PAYLOAD = {
    "observations": [
        {"date": "2026-01-01", "value": "5.33"},
        {"date": "2026-02-01", "value": "."},  # FRED's own missing-data marker
        {"date": "2026-03-01", "value": "5.25"},
    ]
}


def _provider_with_transport(handler, api_key: str = "test-key") -> FredProvider:
    provider = FredProvider(api_key=api_key)
    provider._client = httpx.AsyncClient(
        base_url=provider._client.base_url, transport=httpx.MockTransport(handler)
    )
    return provider


@pytest.mark.asyncio
async def test_get_series_returns_raw_observations():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["series_id"] == "FEDFUNDS"
        assert request.url.params["api_key"] == "test-key"
        return httpx.Response(200, json=OBSERVATIONS_PAYLOAD)

    provider = _provider_with_transport(handler)
    observations = await provider.get_series("FEDFUNDS")

    assert len(observations) == 3
    assert observations[0]["date"] == "2026-01-01"
    await provider.aclose()


@pytest.mark.asyncio
async def test_get_series_without_api_key_raises_immediately():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=OBSERVATIONS_PAYLOAD)

    provider = _provider_with_transport(handler, api_key="")
    with pytest.raises(FredProviderError):
        await provider.get_series("FEDFUNDS")
    assert calls["n"] == 0  # never even made the request
    await provider.aclose()


@pytest.mark.asyncio
async def test_non_retryable_error_raises_immediately():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400, text="bad series id")

    provider = _provider_with_transport(handler)
    with pytest.raises(FredProviderError):
        await provider.get_series("NOT_REAL")
    assert calls["n"] == 1
    await provider.aclose()


@pytest.mark.asyncio
async def test_retryable_error_then_success():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503, text="unavailable")
        return httpx.Response(200, json=OBSERVATIONS_PAYLOAD)

    provider = _provider_with_transport(handler)
    observations = await provider.get_series("FEDFUNDS")
    assert len(observations) == 3
    assert calls["n"] == 3
    await provider.aclose()
