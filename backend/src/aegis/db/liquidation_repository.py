"""Raw storage + windowed read-back for the Liquidation Engine (Phase 4b).

Reads use TimescaleDB's `time_bucket()` to aggregate raw events into fixed
windows server-side - the resulting per-bucket notional/count series feeds
the exact same `aegis.derivatives.analytics` functions (pct_change/zscore/
acceleration) already validated in Phase 4, no new statistics code needed.
"""
from __future__ import annotations

from datetime import UTC, datetime

import asyncpg
import pandas as pd

from aegis.providers.binance.models import LiquidationEvent


def _ms_to_dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000.0, tz=UTC)


class LiquidationRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def insert_events(self, events: list[LiquidationEvent]) -> None:
        if not events:
            return
        rows = [
            (e.symbol, e.side, _ms_to_dt(e.event_time_ms), e.quantity, e.price,
             e.average_price, e.order_status, e.source)
            for e in events
        ]
        async with self._pool.acquire() as conn:
            await conn.executemany(
                """
                INSERT INTO liquidations (symbol, side, time, quantity, price,
                                           average_price, order_status, source)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
                ON CONFLICT (symbol, time, side, quantity, price) DO NOTHING
                """,
                rows,
            )

    async def fetch_windowed_stats(
        self, symbol: str, window_seconds: int, num_buckets: int
    ) -> pd.DataFrame:
        """One row per `window_seconds`-wide bucket, oldest first, covering
        the trailing `window_seconds * num_buckets` of history."""
        query = """
            SELECT
                time_bucket(make_interval(secs => $2::int), time) AS bucket_time,
                COALESCE(SUM(quantity * average_price) FILTER (WHERE side = 'SELL'), 0) AS long_notional,
                COALESCE(SUM(quantity * average_price) FILTER (WHERE side = 'BUY'), 0) AS short_notional,
                COUNT(*) FILTER (WHERE side = 'SELL') AS long_count,
                COUNT(*) FILTER (WHERE side = 'BUY') AS short_count
            FROM liquidations
            WHERE symbol = $1 AND time >= now() - make_interval(secs => ($2::int * $3::int))
            GROUP BY bucket_time
            ORDER BY bucket_time
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query, symbol, window_seconds, num_buckets)
        columns = ["bucket_time", "long_notional", "short_notional", "long_count", "short_count"]
        if not rows:
            return pd.DataFrame(columns=columns)
        return pd.DataFrame([dict(r) for r in rows], columns=columns)
