"""Persistence for the News Engine (Phase 6/6b): source registry,
classified items (re-classifiable in place), asset mentions, and the
per-asset NEWS_CONFLICT verdict.
"""
from __future__ import annotations

from datetime import datetime

import asyncpg

from aegis.news.conflict import AssetNewsStatus, NewsItemForConflict
from aegis.news.service import ClassifiedNewsItem
from aegis.news.source_config import NewsSourceConfig


class NewsRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def upsert_source_registry(self, configs: list[NewsSourceConfig]) -> None:
        if not configs:
            return
        rows = [(c.source_id, c.name, c.feed_url, c.format, c.source_quality) for c in configs]
        async with self._pool.acquire() as conn:
            await conn.executemany(
                """
                INSERT INTO news_sources (source_id, name, feed_url, format, source_quality)
                VALUES ($1,$2,$3,$4,$5)
                ON CONFLICT (source_id) DO UPDATE SET
                    name = EXCLUDED.name, feed_url = EXCLUDED.feed_url,
                    format = EXCLUDED.format, source_quality = EXCLUDED.source_quality,
                    updated_at = now()
                """,
                rows,
            )

    async def insert_news_items(self, items: list[ClassifiedNewsItem]) -> None:
        if not items:
            return
        news_rows = [
            (i.source_id, i.guid, i.title, i.link, i.summary, i.published_at,
             i.sentiment, i.sentiment_score, i.confidence, i.magnitude, i.source_quality_score)
            for i in items
        ]
        entity_rows = [
            (i.source_id, i.guid, asset) for i in items for asset in i.assets
        ]
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.executemany(
                """
                INSERT INTO news (source_id, guid, title, link, summary, published_at,
                                   sentiment, sentiment_score, confidence, magnitude, source_quality_score)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
                ON CONFLICT (source_id, guid) DO UPDATE SET
                    title = EXCLUDED.title, link = EXCLUDED.link, summary = EXCLUDED.summary,
                    published_at = EXCLUDED.published_at, sentiment = EXCLUDED.sentiment,
                    sentiment_score = EXCLUDED.sentiment_score, confidence = EXCLUDED.confidence,
                    magnitude = EXCLUDED.magnitude, source_quality_score = EXCLUDED.source_quality_score
                """,
                news_rows,
            )
            if entity_rows:
                await conn.executemany(
                    """
                    INSERT INTO news_entities (source_id, guid, asset)
                    VALUES ($1,$2,$3)
                    ON CONFLICT (source_id, guid, asset) DO NOTHING
                    """,
                    entity_rows,
                )

    async def fetch_recent(self, source_id: str | None = None, limit: int = 20) -> list[dict]:
        if source_id:
            query = """
                SELECT source_id, guid, title, sentiment, sentiment_score, magnitude,
                       source_quality_score, published_at
                FROM news WHERE source_id = $1
                ORDER BY COALESCE(published_at, ingested_at) DESC LIMIT $2
            """
            args = (source_id, limit)
        else:
            query = """
                SELECT source_id, guid, title, sentiment, sentiment_score, magnitude,
                       source_quality_score, published_at
                FROM news
                ORDER BY COALESCE(published_at, ingested_at) DESC LIMIT $1
            """
            args = (limit,)
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query, *args)
        return [dict(r) for r in rows]

    async def fetch_recent_by_asset(self, asset: str, window_hours: int) -> list[NewsItemForConflict]:
        query = """
            SELECT n.source_id, ns.source_quality, n.sentiment, n.published_at
            FROM news n
            JOIN news_entities ne ON ne.source_id = n.source_id AND ne.guid = n.guid
            JOIN news_sources ns ON ns.source_id = n.source_id
            WHERE ne.asset = $1
              AND COALESCE(n.published_at, n.ingested_at) >= now() - make_interval(hours => $2::int)
            ORDER BY n.published_at
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query, asset, window_hours)
        return [
            NewsItemForConflict(source_id=r["source_id"], source_quality=r["source_quality"],
                                 sentiment=r["sentiment"], published_at=r["published_at"])
            for r in rows
        ]

    async def fetch_all_asset_statuses(self) -> list[dict]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT asset, status, distinct_sources, item_count, dominant_sentiment, computed_at "
                "FROM news_asset_status ORDER BY asset"
            )
        return [dict(r) for r in rows]

    async def get_asset_status(self, asset: str) -> AssetNewsStatus | None:
        """Current verdict only - `news_asset_status` is a single upserted
        row per asset (spec section 41's live state), not a time series.
        Correct for a live engine reading "what does the news say right
        now"; NOT safe to use inside a historical replay (Backtest Engine)
        without a lookahead check, since it always reflects the present
        moment regardless of what point in the past is being evaluated -
        see aegis/strategy/strategies.py's EVENT_REACTION docstring."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT asset, status, distinct_sources, item_count, dominant_sentiment "
                "FROM news_asset_status WHERE asset = $1",
                asset,
            )
        if row is None:
            return None
        return AssetNewsStatus(
            asset=row["asset"], status=row["status"], distinct_sources=row["distinct_sources"],
            item_count=row["item_count"], dominant_sentiment=row["dominant_sentiment"],
        )

    async def upsert_asset_status(self, status: AssetNewsStatus, window_hours: int) -> None:
        if status.status == "NO_NEWS":  # nothing to persist - no row is itself informative
            return
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(
                """
                INSERT INTO news_asset_status (asset, status, distinct_sources, item_count,
                                                dominant_sentiment, window_hours)
                VALUES ($1,$2,$3,$4,$5,$6)
                ON CONFLICT (asset) DO UPDATE SET
                    status = EXCLUDED.status, distinct_sources = EXCLUDED.distinct_sources,
                    item_count = EXCLUDED.item_count, dominant_sentiment = EXCLUDED.dominant_sentiment,
                    window_hours = EXCLUDED.window_hours, computed_at = now()
                """,
                status.asset, status.status, status.distinct_sources, status.item_count,
                status.dominant_sentiment, window_hours,
            )
            # Same values, appended instead of overwritten - see
            # news_asset_status_history's migration docstring for why a
            # second write is worth it (this table is what makes
            # EVENT_REACTION backtestable without a lookahead bug).
            await conn.execute(
                """
                INSERT INTO news_asset_status_history
                    (asset, status, distinct_sources, item_count, dominant_sentiment, window_hours)
                VALUES ($1,$2,$3,$4,$5,$6)
                """,
                status.asset, status.status, status.distinct_sources, status.item_count,
                status.dominant_sentiment, window_hours,
            )

    async def fetch_status_history(
        self, asset: str, start: datetime, end: datetime,
    ) -> list[tuple[datetime, AssetNewsStatus]]:
        """Every status transition for `asset` with `computed_at` in
        [start, end] - the raw material for `aegis.news.conflict.
        most_recent_status_as_of`'s point-in-time lookup. Includes the row
        immediately before `start` too (if any), so a caller replaying a
        window that begins mid-status doesn't wrongly see "no status" for
        its first bars."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                (SELECT computed_at, asset, status, distinct_sources, item_count, dominant_sentiment
                 FROM news_asset_status_history
                 WHERE asset = $1 AND computed_at < $2
                 ORDER BY computed_at DESC LIMIT 1)
                UNION ALL
                (SELECT computed_at, asset, status, distinct_sources, item_count, dominant_sentiment
                 FROM news_asset_status_history
                 WHERE asset = $1 AND computed_at >= $2 AND computed_at <= $3
                 ORDER BY computed_at)
                """,
                asset, start, end,
            )
        return [
            (r["computed_at"], AssetNewsStatus(
                asset=r["asset"], status=r["status"], distinct_sources=r["distinct_sources"],
                item_count=r["item_count"], dominant_sentiment=r["dominant_sentiment"],
            ))
            for r in rows
        ]
