"""CoinMarketCal REST client - crypto-specific events (listings, unlocks,
mainnet launches, hard forks, etc.), spec's "Event Risk" concept for the
crypto-native half (macro FOMC/CPI-style events have no free source - see
aegis/events/service.py's module docstring).

Free tier, verified live 2026-09-23 against the real API (base URL,
`x-api-key` header, `/v2/events` endpoint, response shape) - not built from
docs alone, since coinmarketcal.com's marketing/docs site sits behind
Cloudflare bot protection that blocked automated access entirely; the
actual `api.coinmarketcal.com` subdomain has no such block. Free tier:
3000 requests/month, no `impact`/`impactSummary`/`sourceUrl`/`snapshotUrl`
(those fields exist in the response but are always null - Pro+ only), no
`/v2/categories` access (also Pro+ only, so this client never filters by
category). One request costs one credit regardless of how many coins are
in the `coins` filter - confirmed via `/v2/usage` before and after a
2-coin query.
"""
from __future__ import annotations

import asyncio
from typing import Any

import httpx

from aegis.logging_utils import get_logger, log_event
from aegis.utils.backoff import BackoffPolicy

_LOG = get_logger("coinmarketcal.client")

BASE_URL = "https://api.coinmarketcal.com"
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
MAX_RETRIES = 4


class CoinMarketCalProviderError(RuntimeError):
    pass


class CoinMarketCalProvider:
    def __init__(self, api_key: str, timeout: float = 10.0) -> None:
        self.api_key = api_key
        self._client = httpx.AsyncClient(base_url=BASE_URL, timeout=timeout)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "CoinMarketCalProvider":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def get_events(self, coins: list[str], limit: int = 50) -> list[dict[str, Any]]:
        """One page of upcoming/recent events mentioning any of `coins`
        (canonical CoinMarketCal slugs, e.g. "bitcoin" - tickers collide
        per the API's own docs, slugs don't). Never paginates past this one
        page - the free tier's 3000 req/month budget and this engine's use
        case (what's coming up soon, not a full historical crawl) don't
        need it; `meta.total` in the raw response tells a caller if more
        exist."""
        if not self.api_key:
            raise CoinMarketCalProviderError(
                "COINMARKETCAL_API_KEY is not configured - register for free at "
                "https://coinmarketcal.com/developer"
            )
        params: dict[str, Any] = {"coins": ",".join(coins), "limit": limit}
        headers = {"x-api-key": self.api_key}

        policy = BackoffPolicy(base_seconds=0.5, max_seconds=8.0)
        last_error: Exception | None = None
        for _ in range(MAX_RETRIES):
            try:
                response = await self._client.get("/v2/events", params=params, headers=headers)
            except httpx.TransportError as exc:
                last_error = exc
                log_event(_LOG, "coinmarketcal_transport_error", level=30, error=str(exc))
            else:
                if response.status_code == 200:
                    payload = response.json()
                    return payload.get("data", [])
                if response.status_code not in _RETRYABLE_STATUS:
                    raise CoinMarketCalProviderError(
                        f"GET /v2/events -> HTTP {response.status_code}: {response.text[:300]}"
                    )
                last_error = CoinMarketCalProviderError(f"GET /v2/events -> HTTP {response.status_code}")
                log_event(_LOG, "coinmarketcal_retryable_error", level=30, status=response.status_code)
            await asyncio.sleep(policy.next_delay())
        raise CoinMarketCalProviderError(f"GET /v2/events failed after {MAX_RETRIES} attempts") from last_error
