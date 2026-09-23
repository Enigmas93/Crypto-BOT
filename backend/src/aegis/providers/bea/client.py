"""BEA (Bureau of Economic Analysis) Data API client - spec section 35.

Unlike FRED/BLS, BEA has no flat "series ID" - a value only exists as
(dataset, table, a SeriesCode *within* that table's response). A key
(UserID) is mandatory for every request, free self-service signup at
https://apps.bea.gov/API/signup/index.cfm. Implements `MacroDataProvider`
loosely (its `get_series` needs more context than just an id - see
`aegis.macro.service.MacroEngine._fetch_raw`, which is why this is the one
provider MacroEngine calls with extra keyword arguments).
"""
from __future__ import annotations

import asyncio
import datetime as dt
from typing import Any

import httpx

from aegis.logging_utils import get_logger, log_event
from aegis.providers.base import MacroDataProvider
from aegis.utils.backoff import BackoffPolicy

_LOG = get_logger("bea.client")

BEA_BASE_URL = "https://apps.bea.gov/api/data"
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
MAX_RETRIES = 4


class BeaProviderError(RuntimeError):
    pass


class BeaProvider(MacroDataProvider):
    def __init__(self, api_key: str, years_back: int = 10, timeout: float = 10.0) -> None:
        self.api_key = api_key
        self.years_back = years_back
        self._client = httpx.AsyncClient(base_url=BEA_BASE_URL, timeout=timeout)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "BeaProvider":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def get_series(
        self, series_id: str, dataset_name: str, table_name: str, frequency: str,
        years: str | None = None,
    ) -> list[dict[str, Any]]:
        if not self.api_key:
            raise BeaProviderError(
                "BEA_API_KEY is not configured - register for free at "
                "https://apps.bea.gov/API/signup/index.cfm"
            )
        if years is None:
            current_year = dt.date.today().year
            years = ",".join(str(y) for y in range(current_year - self.years_back, current_year + 1))

        params: dict[str, Any] = {
            "UserID": self.api_key,
            "method": "GetData",
            "DataSetName": dataset_name,
            "TableName": table_name,
            "Frequency": frequency,
            "Year": years,
            "ResultFormat": "JSON",
        }

        policy = BackoffPolicy(base_seconds=0.5, max_seconds=8.0)
        last_error: Exception | None = None
        for _ in range(MAX_RETRIES):
            try:
                response = await self._client.get("", params=params)
            except httpx.TransportError as exc:
                last_error = exc
                log_event(_LOG, "bea_transport_error", level=30, series_id=series_id, error=str(exc))
            else:
                if response.status_code == 200:
                    return self._extract_rows(series_id, response.json())
                if response.status_code not in _RETRYABLE_STATUS:
                    raise BeaProviderError(f"{series_id} -> HTTP {response.status_code}: {response.text[:300]}")
                last_error = BeaProviderError(f"{series_id} -> HTTP {response.status_code}")
                log_event(_LOG, "bea_retryable_error", level=30, series_id=series_id, status=response.status_code)
            await asyncio.sleep(policy.next_delay())
        raise BeaProviderError(f"{series_id} failed after {MAX_RETRIES} attempts") from last_error

    @staticmethod
    def _extract_rows(series_id: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
        api_response = payload.get("BEAAPI", {})
        error = api_response.get("Error")
        if error:
            raise BeaProviderError(f"{series_id} -> BEA error: {error}")
        results = api_response.get("Results", {})
        # a GetData error (bad table/dataset name) comes back as
        # Results.Error rather than a top-level BEAAPI.Error
        if isinstance(results, dict) and results.get("Error"):
            raise BeaProviderError(f"{series_id} -> BEA error: {results['Error']}")
        rows = results.get("Data", []) if isinstance(results, dict) else []
        # a BEA table response contains every line item in that table -
        # filter down to the one series this MacroSeriesConfig asked for
        return [row for row in rows if row.get("SeriesCode") == series_id]
