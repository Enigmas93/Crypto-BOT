"""Raw storage + read-back for the Derivatives Engine (Phase 4).

Symmetric with `candle_repository.py`: writes are batched/idempotent
(`ON CONFLICT DO NOTHING` - these are immutable point-in-time observations,
never revised), reads return ascending pandas Series ready for the
z-score/delta math in `aegis.derivatives.service`.
"""
from __future__ import annotations

from datetime import UTC, datetime

import asyncpg
import pandas as pd

from aegis.providers.binance.models import LongShortRatioPoint, OpenInterestHistPoint


def _ms_to_dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000.0, tz=UTC)


class DerivativesRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def insert_open_interest(self, points: list[OpenInterestHistPoint]) -> None:
        if not points:
            return
        rows = [
            (p.symbol, p.period, _ms_to_dt(p.timestamp_ms), p.sum_open_interest,
             p.sum_open_interest_value, p.source)
            for p in points
        ]
        async with self._pool.acquire() as conn:
            await conn.executemany(
                """
                INSERT INTO open_interest (symbol, period, time, sum_open_interest,
                                            sum_open_interest_value, source)
                VALUES ($1,$2,$3,$4,$5,$6)
                ON CONFLICT (symbol, period, time) DO NOTHING
                """,
                rows,
            )

    async def insert_long_short_ratios(self, points: list[LongShortRatioPoint]) -> None:
        if not points:
            return
        rows = [
            (p.symbol, p.ratio_type, p.period, _ms_to_dt(p.timestamp_ms),
             p.long_short_ratio, p.long_account, p.short_account, p.source)
            for p in points
        ]
        async with self._pool.acquire() as conn:
            await conn.executemany(
                """
                INSERT INTO long_short_ratios (symbol, ratio_type, period, time,
                                                long_short_ratio, long_account, short_account, source)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
                ON CONFLICT (symbol, ratio_type, period, time) DO NOTHING
                """,
                rows,
            )

    async def fetch_open_interest_series(self, symbol: str, period: str, limit: int = 30) -> pd.DataFrame:
        query = """
            SELECT time, sum_open_interest, sum_open_interest_value
            FROM open_interest
            WHERE symbol = $1 AND period = $2
            ORDER BY time DESC
            LIMIT $3
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query, symbol, period, limit)
        if not rows:
            return pd.DataFrame(columns=["time", "sum_open_interest", "sum_open_interest_value"])
        df = pd.DataFrame([dict(r) for r in rows])
        return df.iloc[::-1].reset_index(drop=True)

    async def fetch_long_short_series(
        self, symbol: str, ratio_type: str, period: str, limit: int = 30
    ) -> pd.DataFrame:
        query = """
            SELECT time, long_short_ratio, long_account, short_account
            FROM long_short_ratios
            WHERE symbol = $1 AND ratio_type = $2 AND period = $3
            ORDER BY time DESC
            LIMIT $4
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query, symbol, ratio_type, period, limit)
        if not rows:
            return pd.DataFrame(columns=["time", "long_short_ratio", "long_account", "short_account"])
        df = pd.DataFrame([dict(r) for r in rows])
        return df.iloc[::-1].reset_index(drop=True)

    async def fetch_funding_series(self, symbol: str, limit: int = 100) -> pd.DataFrame:
        """Funding/mark/index price has streamed since Phase 1 (`funding_rates`
        table) - no new polling needed, just reading it back."""
        query = """
            SELECT event_time, mark_price, index_price, funding_rate
            FROM funding_rates
            WHERE symbol = $1
            ORDER BY event_time DESC
            LIMIT $2
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query, symbol, limit)
        if not rows:
            return pd.DataFrame(columns=["event_time", "mark_price", "index_price", "funding_rate"])
        df = pd.DataFrame([dict(r) for r in rows])
        return df.iloc[::-1].reset_index(drop=True)
