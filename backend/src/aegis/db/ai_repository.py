"""Persistence for the AI layer, listing radar and strategy research evidence."""
from __future__ import annotations

import json
from datetime import datetime

import asyncpg

from aegis.ai.analysts import NewsAnalysis

TRADE_TABLES = {"shadow": "shadow_trades", "momentum": "momentum_trades", "paper": "paper_trades"}


class AiRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    # -- news ------------------------------------------------------------
    async def fetch_news_pending_analysis(self, limit: int = 10, max_age_hours: int = 48) -> list[dict]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT n.source_id, n.guid, n.title, n.summary, ns.name AS source_name
                FROM news n
                JOIN news_sources ns ON ns.source_id = n.source_id
                LEFT JOIN ai_news_analysis a ON a.source_id = n.source_id AND a.guid = n.guid
                WHERE a.guid IS NULL
                  AND COALESCE(n.published_at, n.ingested_at) >= now() - make_interval(hours => $2::int)
                ORDER BY COALESCE(n.published_at, n.ingested_at) DESC
                LIMIT $1
                """,
                limit, max_age_hours,
            )
        return [dict(r) for r in rows]

    async def upsert_news_analysis(self, source_id: str, guid: str, model: str, a: NewsAnalysis) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO ai_news_analysis (source_id, guid, model, sentiment, magnitude, confidence,
                                               event_type, assets, summary_pt)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
                ON CONFLICT (source_id, guid) DO UPDATE SET
                    model = EXCLUDED.model, sentiment = EXCLUDED.sentiment, magnitude = EXCLUDED.magnitude,
                    confidence = EXCLUDED.confidence, event_type = EXCLUDED.event_type,
                    assets = EXCLUDED.assets, summary_pt = EXCLUDED.summary_pt, analyzed_at = now()
                """,
                source_id, guid, model, a.sentiment, a.magnitude, a.confidence, a.event_type, a.assets, a.summary_pt,
            )

    async def fetch_recent_news_with_ai(self, limit: int = 60) -> list[dict]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT n.source_id, n.title, n.link, COALESCE(n.published_at, n.ingested_at) AS published_at,
                       n.sentiment AS keyword_sentiment, a.sentiment AS ai_sentiment, a.magnitude AS ai_magnitude,
                       a.confidence AS ai_confidence, a.event_type, a.assets AS ai_assets, a.summary_pt, a.model
                FROM news n
                LEFT JOIN ai_news_analysis a ON a.source_id = n.source_id AND a.guid = n.guid
                ORDER BY COALESCE(n.published_at, n.ingested_at) DESC
                LIMIT $1
                """,
                limit,
            )
        return [dict(r) for r in rows]

    async def ai_news_pulse(self, hours: int = 24) -> list[dict]:
        """Per-asset sentiment balance from AI-read news, magnitude-weighted."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT asset,
                       count(*) AS items,
                       sum(CASE a.sentiment WHEN 'positive' THEN a.magnitude WHEN 'negative' THEN -a.magnitude
                           ELSE 0 END) AS score,
                       max(a.magnitude) AS max_magnitude
                FROM ai_news_analysis a
                JOIN news n ON n.source_id = a.source_id AND n.guid = a.guid
                CROSS JOIN LATERAL unnest(a.assets) AS asset
                WHERE COALESCE(n.published_at, n.ingested_at) >= now() - make_interval(hours => $1::int)
                GROUP BY asset ORDER BY count(*) DESC LIMIT 30
                """,
                hours,
            )
        return [dict(r) for r in rows]

    # -- insights --------------------------------------------------------
    async def insert_insight(self, kind: str, subject: str, model: str, payload: dict) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO ai_insights (kind, subject, model, payload) VALUES ($1,$2,$3,$4::jsonb)",
                kind, subject, model, json.dumps(payload, default=str),
            )

    async def insight_exists(self, kind: str, subject: str) -> bool:
        async with self._pool.acquire() as conn:
            return bool(await conn.fetchval(
                "SELECT 1 FROM ai_insights WHERE kind = $1 AND subject = $2 LIMIT 1", kind, subject,
            ))

    async def last_insight_time(self, kind: str, subject: str) -> datetime | None:
        async with self._pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT max(created_at) FROM ai_insights WHERE kind = $1 AND subject = $2", kind, subject,
            )

    async def latest_insights(self, kind: str, limit: int = 20) -> list[dict]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT subject, model, payload, created_at FROM ai_insights WHERE kind = $1 "
                "ORDER BY created_at DESC LIMIT $2",
                kind, limit,
            )
        return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]

    async def latest_insight_per_subject(self, kind: str) -> list[dict]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT DISTINCT ON (subject) subject, model, payload, created_at
                FROM ai_insights WHERE kind = $1 ORDER BY subject, created_at DESC
                """,
                kind,
            )
        return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]

    # -- trades awaiting a review -----------------------------------------
    async def fetch_trades_pending_review(self, limit: int = 3, max_age_days: int = 7) -> list[dict]:
        parts = []
        for key, table in TRADE_TABLES.items():
            parts.append(
                f"""
                SELECT '{key}' AS engine, t.id, t.account_id, t.symbol, t.side, t.entry_time, t.entry_price,
                       t.exit_time, t.exit_price, t.exit_reason, t.net_pnl, t.r_multiple, t.confluence_score,
                       t.reasons, t.mfe_r, t.mae_r
                FROM {table} t
                WHERE t.exit_time >= now() - make_interval(days => $2::int)
                  AND NOT EXISTS (SELECT 1 FROM ai_insights i WHERE i.kind = 'TRADE_REVIEW'
                                  AND i.subject = '{key}:' || t.id)
                """
            )
        query = " UNION ALL ".join(parts) + " ORDER BY exit_time DESC LIMIT $1"
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query, limit, max_age_days)
        return [dict(r) for r in rows]

    # -- listing radar -----------------------------------------------------
    async def insert_listing_event(self, source: str, ticker: str, title: str, announced_at: datetime,
                                   bingx_symbol: str | None, price: float | None) -> bool:
        async with self._pool.acquire() as conn:
            inserted = await conn.fetchval(
                """
                INSERT INTO listing_events (source, ticker, title, announced_at, bingx_symbol, price_at_detection)
                VALUES ($1,$2,$3,$4,$5,$6)
                ON CONFLICT ON CONSTRAINT uq_listing_events DO NOTHING
                RETURNING id
                """,
                source, ticker, title, announced_at, bingx_symbol, price,
            )
        return inserted is not None

    async def recent_listing_events(self, limit: int = 40) -> list[dict]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT source, ticker, title, announced_at, bingx_symbol, price_at_detection, detected_at "
                "FROM listing_events ORDER BY announced_at DESC LIMIT $1",
                limit,
            )
        return [dict(r) for r in rows]

    # -- research evidence -------------------------------------------------
    async def insert_research_run(self, name: str, data_source: str, interval: str, config: dict,
                                  metrics: dict) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO strategy_research_runs (name, data_source, interval, config, metrics) "
                "VALUES ($1,$2,$3,$4::jsonb,$5::jsonb)",
                name, data_source, interval, json.dumps(config), json.dumps(metrics),
            )

    async def latest_research_runs(self, limit: int = 30) -> list[dict]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT DISTINCT ON (name, data_source, interval) name, data_source, interval, config, metrics,
                       created_at
                FROM strategy_research_runs ORDER BY name, data_source, interval, created_at DESC
                """
            )
        out = [{**dict(r), "config": json.loads(r["config"]), "metrics": json.loads(r["metrics"])} for r in rows]
        return out[:limit]
