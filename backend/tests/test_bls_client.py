import json

import httpx
import pytest

from aegis.providers.bls.client import BlsProvider, BlsProviderError

SUCCESS_PAYLOAD = {
    "status": "REQUEST_SUCCEEDED",
    "responseTime": 100,
    "message": [],
    "Results": {
        "series": [
            {
                "seriesID": "LNS14000000",
                "data": [
                    {"year": "2026", "period": "M08", "periodName": "August", "value": "4.1", "footnotes": [{}]},
                    {"year": "2026", "period": "M07", "periodName": "July", "value": "4.2", "footnotes": [{}]},
                ],
            }
        ]
    },
}


def _provider_with_transport(handler, api_key: str = "") -> BlsProvider:
    provider = BlsProvider(api_key=api_key)
    provider._client = httpx.AsyncClient(
        base_url=provider._client.base_url, transport=httpx.MockTransport(handler)
    )
    return provider


@pytest.mark.asyncio
async def test_get_series_returns_data_points_and_works_without_a_key():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["seriesid"] == ["LNS14000000"]
        assert "registrationkey" not in body  # no key configured -> omitted, not sent empty
        return httpx.Response(200, json=SUCCESS_PAYLOAD)

    provider = _provider_with_transport(handler, api_key="")
    data = await provider.get_series("LNS14000000")

    assert len(data) == 2
    assert data[0]["period"] == "M08"
    await provider.aclose()


@pytest.mark.asyncio
async def test_get_series_includes_registration_key_when_configured():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["registrationkey"] == "my-key"
        return httpx.Response(200, json=SUCCESS_PAYLOAD)

    provider = _provider_with_transport(handler, api_key="my-key")
    await provider.get_series("LNS14000000")
    await provider.aclose()


@pytest.mark.asyncio
async def test_default_year_range_spans_history_years_setting():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json=SUCCESS_PAYLOAD)

    provider = BlsProvider(api_key="", history_years=5)
    provider._client = httpx.AsyncClient(base_url=provider._client.base_url, transport=httpx.MockTransport(handler))
    await provider.get_series("LNS14000000")

    assert int(captured["endyear"]) - int(captured["startyear"]) == 5
    await provider.aclose()


@pytest.mark.asyncio
async def test_bls_error_status_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "REQUEST_NOT_PROCESSED", "message": ["bad series id"],
                                          "Results": {}})

    provider = _provider_with_transport(handler)
    with pytest.raises(BlsProviderError):
        await provider.get_series("NOT_REAL")
    await provider.aclose()


@pytest.mark.asyncio
async def test_non_retryable_http_error_raises_immediately():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400, text="bad request")

    provider = _provider_with_transport(handler)
    with pytest.raises(BlsProviderError):
        await provider.get_series("LNS14000000")
    assert calls["n"] == 1
    await provider.aclose()


@pytest.mark.asyncio
async def test_retryable_error_then_success():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503, text="unavailable")
        return httpx.Response(200, json=SUCCESS_PAYLOAD)

    provider = _provider_with_transport(handler)
    data = await provider.get_series("LNS14000000")
    assert len(data) == 2
    assert calls["n"] == 3
    await provider.aclose()
