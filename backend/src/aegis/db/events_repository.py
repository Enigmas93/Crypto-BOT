"""Persistence for the Event Risk Engine (Fase 15b, CoinMarketCal) - one
upserted row per event, keyed by CoinMarketCal's own event id (dates get
refined over time - see migration 0017's docstring for why this isn't
append-only like `news`).
"""
from __future__ import annotations

from datetime import datetime

import asyncpg

from aegis.events.models import CoinMarketCalEvent


class EventsRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def upsert_events(self, events: list[CoinMarketCalEvent]) -> None:
        if not events:
            return
        rows = [
            (e.event_id, e.slug, e.title, e.description, e.date, e.date_end, e.date_type,
             e.is_estimated, e.displayed_date, e.coins, e.impact, e.impact_summary,
             e.source_url, e.snapshot_url, e.last_verified_at, e.created_at, e.updated_at)
            for e in events
        ]
        async with self._pool.acquire() as conn:
            await conn.executemany(
                """
                INSERT INTO coinmarketcal_events (
                    event_id, slug, title, description, date, date_end, date_type,
                    is_estimated, displayed_date, coins, impact, impact_summary,
                    source_url, snapshot_url, last_verified_at, created_at, updated_at
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17)
                ON CONFLICT (event_id) DO UPDATE SET
                    slug = EXCLUDED.slug, title = EXCLUDED.title, description = EXCLUDED.description,
                    date = EXCLUDED.date, date_end = EXCLUDED.date_end, date_type = EXCLUDED.date_type,
                    is_estimated = EXCLUDED.is_estimated, displayed_date = EXCLUDED.displayed_date,
                    coins = EXCLUDED.coins, impact = EXCLUDED.impact, impact_summary = EXCLUDED.impact_summary,
                    source_url = EXCLUDED.source_url, snapshot_url = EXCLUDED.snapshot_url,
                    last_verified_at = EXCLUDED.last_verified_at, updated_at = EXCLUDED.updated_at,
                    ingested_at = now()
                """,
                rows,
            )

    async def fetch_upcoming(self, since: datetime, limit: int = 50) -> list[dict]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM coinmarketcal_events WHERE date >= $1 ORDER BY date ASC LIMIT $2",
                since, limit,
            )
        return [dict(r) for r in rows]
