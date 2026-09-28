"""CandleRepository-compatible candle source backed by an exchange's REST
klines endpoint, so an engine can compute signals from the SAME exchange it
executes on (Shadow BingX: BingX candles, not Binance's).

Only re-fetches once a new bar should have closed, so polling every 30s
costs one request per symbol per bar, not one per poll.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd

from aegis.momentum.candles import klines_to_closed_dataframe

_INTERVAL = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "2h": 120, "4h": 240, "6h": 360,
             "12h": 720, "1d": 1440}


class RestCandleSource:
    def __init__(self, rest_client) -> None:
        self.rest = rest_client
        self._cache: dict[tuple[str, str, int], pd.DataFrame] = {}

    async def fetch_ohlcv(self, symbol: str, interval: str, limit: int = 500, closed_only: bool = True) -> pd.DataFrame:
        key = (symbol, interval, limit)
        cached = self._cache.get(key)
        if cached is not None and not cached.empty:
            next_close = cached["close_time"].iloc[-1] + timedelta(minutes=_INTERVAL.get(interval, 1))
            if datetime.now(UTC) < next_close:
                return cached
        # +1 so dropping the still-forming bar still leaves `limit` closed bars.
        klines = await self.rest.get_klines(symbol, interval, limit=limit + 1)
        df = klines_to_closed_dataframe(klines).tail(limit).reset_index(drop=True)
        self._cache[key] = df
        return df
