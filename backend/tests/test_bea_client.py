import httpx
import pytest

from aegis.providers.bea.client import BeaProvider, BeaProviderError

SUCCESS_PAYLOAD = {
    "BEAAPI": {
        "Request": {},
        "Results": {
            "Data": [
                {"TableName": "T10101", "SeriesCode": "A191RL", "LineNumber": "1",
                 "LineDescription": "Gross domestic product", "TimePeriod": "2026Q1", "DataValue": "3.4"},
                {"TableName": "T10101", "SeriesCode": "A191RL", "LineNumber": "1",
                 "LineDescription": "Gross domestic product", "TimePeriod": "2025Q4", "DataValue": "2,100.5"},
                {"TableName": "T10101", "SeriesCode": "DPCERL", "LineNumber": "2",
                 "LineDescription": "Personal consumption expenditures", "TimePeriod": "2026Q1", "DataValue": "2.9"},
            ]
        },
    }
}


def _provider_with_transport(handler, api_key: str = "test-key") -> BeaProvider:
    provider = BeaProvider(api_key=api_key)
    provider._client = httpx.AsyncClient(
        base_url=provider._client.base_url, transport=httpx.MockTransport(handler)
    )
    return provider


@pytest.mark.asyncio
async def test_get_series_filters_to_the_requested_series_code():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["UserID"] == "test-key"
        assert request.url.params["DataSetName"] == "NIPA"
        assert request.url.params["TableName"] == "T10101"
        return httpx.Response(200, json=SUCCESS_PAYLOAD)

    provider = _provider_with_transport(handler)
    rows = await provider.get_series("A191RL", dataset_name="NIPA", table_name="T10101", frequency="Q")

    assert len(rows) == 2  # only A191RL rows, DPCERL filtered out
    assert all(r["SeriesCode"] == "A191RL" for r in rows)
    await provider.aclose()


@pytest.mark.asyncio
async def test_get_series_without_api_key_raises_immediately():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=SUCCESS_PAYLOAD)

    provider = _provider_with_transport(handler, api_key="")
    with pytest.raises(BeaProviderError):
        await provider.get_series("A191RL", dataset_name="NIPA", table_name="T10101", frequency="Q")
    assert calls["n"] == 0
    await provider.aclose()


@pytest.mark.asyncio
async def test_default_years_span_years_back_setting():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["year"] = request.url.params["Year"]
        return httpx.Response(200, json=SUCCESS_PAYLOAD)

    provider = BeaProvider(api_key="k", years_back=5)
    provider._client = httpx.AsyncClient(base_url=provider._client.base_url, transport=httpx.MockTransport(handler))
    await provider.get_series("A191RL", dataset_name="NIPA", table_name="T10101", frequency="Q")

    years = captured["year"].split(",")
    assert len(years) == 6  # years_back=5 -> current year plus 5 prior = 6 years listed
    await provider.aclose()


@pytest.mark.asyncio
async def test_bea_top_level_error_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"BEAAPI": {"Error": {"APIErrorCode": "4", "APIErrorDescription": "bad table"}}})

    provider = _provider_with_transport(handler)
    with pytest.raises(BeaProviderError):
        await provider.get_series("A191RL", dataset_name="NIPA", table_name="NOT_REAL", frequency="Q")
    await provider.aclose()


@pytest.mark.asyncio
async def test_bea_results_level_error_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"BEAAPI": {"Results": {"Error": {"APIErrorDescription": "bad params"}}}})

    provider = _provider_with_transport(handler)
    with pytest.raises(BeaProviderError):
        await provider.get_series("A191RL", dataset_name="NIPA", table_name="T10101", frequency="Q")
    await provider.aclose()


@pytest.mark.asyncio
async def test_non_retryable_http_error_raises_immediately():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400, text="bad request")

    provider = _provider_with_transport(handler)
    with pytest.raises(BeaProviderError):
        await provider.get_series("A191RL", dataset_name="NIPA", table_name="T10101", frequency="Q")
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
    rows = await provider.get_series("A191RL", dataset_name="NIPA", table_name="T10101", frequency="Q")
    assert len(rows) == 2
    assert calls["n"] == 3
    await provider.aclose()
