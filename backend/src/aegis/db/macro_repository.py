"""Persistence for the Macro Engine (Phase 5): series registry, raw
observations (idempotent, but *upserted* not ignored - FRED revises
historical values after initial release, unlike Binance trade data), and
the single computed snapshot per series.
"""
from __future__ import annotations

import asyncpg
import pandas as pd

from aegis.macro.models import MacroObservation
from aegis.macro.series_config import MacroSeriesConfig
from aegis.macro.service import MacroSnapshot


class MacroRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def upsert_series_registry(self, configs: list[MacroSeriesConfig]) -> None:
        if not configs:
            return
        rows = [
            (c.series_id, c.name, c.description, c.frequency, c.source, c.importance, c.transformation)
            for c in configs
        ]
        async with self._pool.acquire() as conn:
            await conn.executemany(
                """
                INSERT INTO macro_series (series_id, name, description, frequency, source, importance, transformation)
                VALUES ($1,$2,$3,$4,$5,$6,$7)
                ON CONFLICT (series_id) DO UPDATE SET
                    name = EXCLUDED.name, description = EXCLUDED.description,
                    frequency = EXCLUDED.frequency, source = EXCLUDED.source,
                    importance = EXCLUDED.importance, transformation = EXCLUDED.transformation,
                    updated_at = now()
                """,
                rows,
            )

    async def insert_observations(self, observations: list[MacroObservation]) -> None:
        if not observations:
            return
        rows = [(o.series_id, o.date, o.value, o.source) for o in observations]
        async with self._pool.acquire() as conn:
            await conn.executemany(
                """
                INSERT INTO macro_observations (series_id, date, value, source)
                VALUES ($1,$2,$3,$4)
                ON CONFLICT (series_id, date) DO UPDATE SET
                    value = EXCLUDED.value, ingested_at = now()
                """,
                rows,
            )

    async def fetch_observations(self, series_id: str, limit: int = 260) -> pd.DataFrame:
        query = """
            SELECT date, value FROM macro_observations
            WHERE series_id = $1
            ORDER BY date DESC
            LIMIT $2
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query, series_id, limit)
        if not rows:
            return pd.DataFrame(columns=["date", "value"])
        df = pd.DataFrame([dict(r) for r in rows], columns=["date", "value"])
        return df.iloc[::-1].reset_index(drop=True)

    async def upsert_snapshot(self, snapshot: MacroSnapshot) -> None:
        if snapshot.as_of is None:  # NO_DATA - nothing to store
            return
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO macro_snapshots (series_id, as_of, value, change_pct,
                                              yoy_pct_change, zscore_vs_trailing, quality)
                VALUES ($1,$2,$3,$4,$5,$6,$7)
                ON CONFLICT (series_id) DO UPDATE SET
                    as_of = EXCLUDED.as_of, value = EXCLUDED.value,
                    change_pct = EXCLUDED.change_pct, yoy_pct_change = EXCLUDED.yoy_pct_change,
                    zscore_vs_trailing = EXCLUDED.zscore_vs_trailing, quality = EXCLUDED.quality,
                    computed_at = now()
                """,
                snapshot.series_id, snapshot.as_of, snapshot.value, snapshot.change_pct,
                snapshot.yoy_pct_change, snapshot.zscore_vs_trailing, snapshot.quality,
            )
