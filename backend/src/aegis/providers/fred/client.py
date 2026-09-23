"""FRED (Federal Reserve Economic Data) REST client - spec section 33.

Free, public series data, one API key required (instant self-service
registration at https://fred.stlouisfed.org/docs/api/api_key.html - no
approval wait). Implements `MacroDataProvider` (providers/base.py).
"""
from __future__ import annotations

import asyncio
from datetime import date, timedelta
from typing import Any

import httpx

from aegis.logging_utils import get_logger, log_event
from aegis.providers.base import MacroDataProvider
from aegis.utils.backoff import BackoffPolicy

_LOG = get_logger("fred.client")

FRED_BASE_URL = "https://api.stlouisfed.org/fred"
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
MAX_RETRIES = 4


class FredProviderError(RuntimeError):
    pass


class FredProvider(MacroDataProvider):
    def __init__(self, api_key: str, history_days: int = 390, timeout: float = 10.0) -> None:
        self.api_key = api_key
        # Without observation_start, FRED returns a series' ENTIRE history
        # (decades, for a daily series like DGS10) on every call. Owning a
        # sane default here - not in whatever calls this provider - keeps
        # "how much history to ask FRED for" a FRED concern, not an
        # orchestration concern. 390 days comfortably covers a year of any
        # frequency this engine supports (daily/weekly/monthly).
        self.history_days = history_days
        self._client = httpx.AsyncClient(base_url=FRED_BASE_URL, timeout=timeout)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "FredProvider":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def get_series(self, series_id: str, observation_start: str | None = None) -> list[dict[str, Any]]:
        if not self.api_key:
            raise FredProviderError(
                "FRED_API_KEY is not configured - register for free at "
                "https://fred.stlouisfed.org/docs/api/api_key.html"
            )
        if observation_start is None:
            observation_start = (date.today() - timedelta(days=self.history_days)).isoformat()
        params: dict[str, Any] = {
            "series_id": series_id,
            "api_key": self.api_key,
            "file_type": "json",
            "sort_order": "asc",
            "observation_start": observation_start,
        }

        policy = BackoffPolicy(base_seconds=0.5, max_seconds=8.0)
        last_error: Exception | None = None
        for _ in range(MAX_RETRIES):
            try:
                response = await self._client.get("/series/observations", params=params)
            except httpx.TransportError as exc:
                last_error = exc
                log_event(_LOG, "fred_transport_error", level=30, series_id=series_id, error=str(exc))
            else:
                if response.status_code == 200:
                    payload = response.json()
                    return payload.get("observations", [])
                if response.status_code not in _RETRYABLE_STATUS:
                    raise FredProviderError(
                        f"{series_id} -> HTTP {response.status_code}: {response.text[:300]}"
                    )
                last_error = FredProviderError(f"{series_id} -> HTTP {response.status_code}")
                log_event(_LOG, "fred_retryable_error", level=30, series_id=series_id, status=response.status_code)
            await asyncio.sleep(policy.next_delay())
        raise FredProviderError(f"{series_id} failed after {MAX_RETRIES} attempts") from last_error
