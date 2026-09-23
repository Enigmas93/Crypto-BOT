"""Reads persisted candles back out as a pandas DataFrame, ready for
`aegis.technical`. The only repository in `db/` that reads (the others are
all write paths for the collector) - a deliberate separation since read and
write access patterns rarely evolve together.
"""
from __future__ import annotations

import pandas as pd
import asyncpg

_COLUMNS = [
    "open_time", "close_time", "open", "high", "low", "close", "volume",
    "quote_volume", "trades", "taker_buy_base_volume", "taker_buy_quote_volume", "is_closed",
]


class CandleRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def fetch_ohlcv(
        self, symbol: str, interval: str, limit: int = 500, closed_only: bool = True
    ) -> pd.DataFrame:
        """Returns candles ascending by open_time (oldest first) - the order
        every indicator function assumes. Empty DataFrame if none exist."""
        closed_clause = "AND is_closed = true" if closed_only else ""
        query = f"""
            SELECT open_time, close_time, open, high, low, close, volume, quote_volume,
                   trades, taker_buy_base_volume, taker_buy_quote_volume, is_closed
            FROM candles
            WHERE symbol = $1 AND interval = $2 {closed_clause}
            ORDER BY open_time DESC
            LIMIT $3
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query, symbol, interval, limit)

        if not rows:
            return pd.DataFrame(columns=_COLUMNS)

        df = pd.DataFrame([dict(r) for r in rows], columns=_COLUMNS)
        return df.iloc[::-1].reset_index(drop=True)
