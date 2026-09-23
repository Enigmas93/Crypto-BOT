"""BLS (Bureau of Labor Statistics) Public Data API v2 client - spec
section 34.

Unlike FRED, a registration key is *optional* here (25 queries/day, 10-year
max span unregistered; 500/day and 20 years with a free key from
https://data.bls.gov/registrationEngine/). Implements `MacroDataProvider`.
"""
from __future__ import annotations

import asyncio
import datetime as dt
from typing import Any

import httpx

from aegis.logging_utils import get_logger, log_event
from aegis.providers.base import MacroDataProvider
from aegis.utils.backoff import BackoffPolicy

_LOG = get_logger("bls.client")

BLS_BASE_URL = "https://api.bls.gov/publicAPI/v2"
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
MAX_RETRIES = 4


class BlsProviderError(RuntimeError):
    pass


class BlsProvider(MacroDataProvider):
    def __init__(self, api_key: str = "", history_years: int = 3, timeout: float = 10.0) -> None:
        self.api_key = api_key
        self.history_years = history_years
        self._client = httpx.AsyncClient(base_url=BLS_BASE_URL, timeout=timeout)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "BlsProvider":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def get_series(
        self, series_id: str, start_year: int | None = None, end_year: int | None = None
    ) -> list[dict[str, Any]]:
        end_year = end_year or dt.date.today().year
        start_year = start_year or (end_year - self.history_years)

        body: dict[str, Any] = {
            "seriesid": [series_id],
            "startyear": str(start_year),
            "endyear": str(end_year),
        }
        if self.api_key:
            body["registrationkey"] = self.api_key

        policy = BackoffPolicy(base_seconds=0.5, max_seconds=8.0)
        last_error: Exception | None = None
        for _ in range(MAX_RETRIES):
            try:
                response = await self._client.post("/timeseries/data/", json=body)
            except httpx.TransportError as exc:
                last_error = exc
                log_event(_LOG, "bls_transport_error", level=30, series_id=series_id, error=str(exc))
            else:
                if response.status_code == 200:
                    payload = response.json()
                    return self._extract_data_points(series_id, payload)
                if response.status_code not in _RETRYABLE_STATUS:
                    raise BlsProviderError(f"{series_id} -> HTTP {response.status_code}: {response.text[:300]}")
                last_error = BlsProviderError(f"{series_id} -> HTTP {response.status_code}")
                log_event(_LOG, "bls_retryable_error", level=30, series_id=series_id, status=response.status_code)
            await asyncio.sleep(policy.next_delay())
        raise BlsProviderError(f"{series_id} failed after {MAX_RETRIES} attempts") from last_error

    @staticmethod
    def _extract_data_points(series_id: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
        status = payload.get("status")
        if status != "REQUEST_SUCCEEDED":
            raise BlsProviderError(f"{series_id} -> BLS status {status}: {payload.get('message')}")
        series_list = payload.get("Results", {}).get("series", [])
        if not series_list:
            return []
        return series_list[0].get("data", [])
