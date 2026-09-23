"""Persists feature snapshots into the shared `market_features` table.

The payload is JSONB, merged on conflict (`features || EXCLUDED.features`)
rather than replaced - this is deliberately the *one* feature-store table
spec section 45 describes. `TechnicalSnapshot` (Phase 3) and
`DerivativesSnapshot` (Phase 4) - and whatever Macro/News/Sentiment engines
add later - all write into the same row for the same (symbol, timeframe,
as_of) instead of creating parallel tables, so a consumer reading a bar's
features always finds everything known about it in one place.

Any snapshot type works here as long as it has `symbol`, `as_of`, `quality`,
`to_features_dict()`, and either `.interval` (Phase 3's term) or `.period`
(Phase 4's term, matching Binance's own vocabulary for these endpoints) -
both name the same concept: which timeframe bucket this snapshot is for.
"""
from __future__ import annotations

import json
from typing import Any

import asyncpg


def _timeframe_of(snapshot: Any) -> str:
    timeframe = getattr(snapshot, "interval", None) or getattr(snapshot, "period", None)
    if timeframe is None:
        raise AttributeError(f"{type(snapshot).__name__} has neither .interval nor .period")
    return timeframe


class FeatureRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def upsert_snapshot(self, snapshot: Any) -> None:
        if snapshot.as_of is None:  # NO_DATA - nothing computed, nothing to store
            return
        features_json = json.dumps(snapshot.to_features_dict())
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO market_features (symbol, interval, as_of, quality, features)
                VALUES ($1, $2, $3, $4, $5::jsonb)
                ON CONFLICT (symbol, interval, as_of) DO UPDATE SET
                    features = market_features.features || EXCLUDED.features,
                    quality = EXCLUDED.quality,
                    computed_at = now()
                """,
                snapshot.symbol, _timeframe_of(snapshot), snapshot.as_of, snapshot.quality, features_json,
            )
