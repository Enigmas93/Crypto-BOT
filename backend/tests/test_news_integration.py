"""Integration tests for NewsRepository against a real TimescaleDB.
Skipped automatically if the database is unreachable (see conftest.py).
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from aegis.db.news_repository import NewsRepository
from aegis.news.service import ClassifiedNewsItem
from aegis.news.source_config import NewsSourceConfig


def _source_id() -> str:
    return f"test_{uuid.uuid4().hex[:8]}"


def _source_config(source_id: str) -> NewsSourceConfig:
    return NewsSourceConfig(source_id=source_id, name="Test Source", feed_url="https://example.gov/feed.rss",
                             format="rss", source_quality="PRIMARY_OFFICIAL")


def _item(source_id: str, guid: str, assets=None, sentiment="neutral") -> ClassifiedNewsItem:
    return ClassifiedNewsItem(
        source_id=source_id, guid=guid, title="Test headline", link="https://example.gov/1",
        summary="Test summary", published_at=datetime(2026, 9, 18, 15, 0, tzinfo=UTC),
        assets=assets or [], sentiment=sentiment, sentiment_score=0.0, confidence=0.0,
        magnitude="LOW", source_quality="PRIMARY_OFFICIAL", source_quality_score=1.0,
    )


@pytest.mark.asyncio
async def test_upsert_source_registry_inserts_then_updates(pool):
    repo = NewsRepository(pool)
    source_id = _source_id()

    await repo.upsert_source_registry([_source_config(source_id)])
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM news_sources WHERE source_id=$1", source_id)
    assert row["source_quality"] == "PRIMARY_OFFICIAL"


@pytest.mark.asyncio
async def test_insert_news_items_dedups_on_source_and_guid(pool):
    repo = NewsRepository(pool)
    source_id = _source_id()
    await repo.upsert_source_registry([_source_config(source_id)])
    item = _item(source_id, "g1")

    await repo.insert_news_items([item])
    await repo.insert_news_items([item])  # exact repeat - re-poll of the same item

    async with pool.acquire() as conn:
        count = await conn.fetchval("SELECT count(*) FROM news WHERE source_id=$1", source_id)
    assert count == 1


@pytest.mark.asyncio
async def test_insert_news_items_reclassification_overwrites_in_place(pool):
    repo = NewsRepository(pool)
    source_id = _source_id()
    await repo.upsert_source_registry([_source_config(source_id)])

    await repo.insert_news_items([_item(source_id, "g1", sentiment="neutral")])
    await repo.insert_news_items([_item(source_id, "g1", sentiment="positive")])

    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT sentiment FROM news WHERE source_id=$1 AND guid=$2", source_id, "g1")
    assert row["sentiment"] == "positive"


@pytest.mark.asyncio
async def test_insert_news_items_writes_entity_rows_for_every_mentioned_asset(pool):
    repo = NewsRepository(pool)
    source_id = _source_id()
    await repo.upsert_source_registry([_source_config(source_id)])

    await repo.insert_news_items([_item(source_id, "g1", assets=["BTC", "ETH"])])

    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT asset FROM news_entities WHERE source_id=$1 AND guid=$2", source_id, "g1")
    assert {r["asset"] for r in rows} == {"BTC", "ETH"}


@pytest.mark.asyncio
async def test_fetch_recent_filters_by_source_and_orders_newest_first(pool):
    repo = NewsRepository(pool)
    source_id = _source_id()
    await repo.upsert_source_registry([_source_config(source_id)])
    older = ClassifiedNewsItem(
        source_id=source_id, guid="old", title="Older", link="x", summary="",
        published_at=datetime(2026, 1, 1, tzinfo=UTC), assets=[], sentiment="neutral",
        sentiment_score=0.0, confidence=0.0, magnitude="LOW",
        source_quality="PRIMARY_OFFICIAL", source_quality_score=1.0,
    )
    newer = _item(source_id, "new")  # published 2026-09-18
    await repo.insert_news_items([older, newer])

    results = await repo.fetch_recent(source_id=source_id, limit=10)

    assert len(results) == 2
    assert results[0]["guid"] == "new"  # newest first


@pytest.mark.asyncio
async def test_fetch_recent_by_asset_joins_entities_and_sources_within_window(pool):
    repo = NewsRepository(pool)
    source_id = _source_id()
    await repo.upsert_source_registry([_source_config(source_id)])
    asset = f"TEST{uuid.uuid4().hex[:6].upper()}"
    now = datetime.now(UTC)

    recent = ClassifiedNewsItem(
        source_id=source_id, guid="recent", title="Recent", link="x", summary="",
        published_at=now, assets=[asset], sentiment="positive", sentiment_score=1.0,
        confidence=1.0, magnitude="HIGH", source_quality="PRIMARY_OFFICIAL", source_quality_score=1.0,
    )
    too_old = ClassifiedNewsItem(
        source_id=source_id, guid="old", title="Old", link="x", summary="",
        published_at=now - timedelta(days=30), assets=[asset], sentiment="negative",
        sentiment_score=-1.0, confidence=1.0, magnitude="HIGH",
        source_quality="PRIMARY_OFFICIAL", source_quality_score=1.0,
    )
    await repo.insert_news_items([recent, too_old])

    items = await repo.fetch_recent_by_asset(asset, window_hours=24)

    assert len(items) == 1
    assert items[0].source_id == source_id
    assert items[0].source_quality == "PRIMARY_OFFICIAL"
    assert items[0].sentiment == "positive"


@pytest.mark.asyncio
async def test_upsert_asset_status_replaces_in_place_and_skips_no_news(pool):
    from aegis.news.conflict import AssetNewsStatus

    repo = NewsRepository(pool)
    asset = f"TEST{uuid.uuid4().hex[:6].upper()}"

    no_news = AssetNewsStatus(asset=asset, status="NO_NEWS", distinct_sources=0, item_count=0,
                               dominant_sentiment=None)
    await repo.upsert_asset_status(no_news, window_hours=24)
    async with pool.acquire() as conn:
        count = await conn.fetchval("SELECT count(*) FROM news_asset_status WHERE asset=$1", asset)
    assert count == 0  # NO_NEWS is never persisted

    conflict = AssetNewsStatus(asset=asset, status="NEWS_CONFLICT", distinct_sources=2, item_count=2,
                                dominant_sentiment=None)
    await repo.upsert_asset_status(conflict, window_hours=24)
    confirmed = AssetNewsStatus(asset=asset, status="CONFIRMED", distinct_sources=2, item_count=3,
                                 dominant_sentiment="positive")
    await repo.upsert_asset_status(confirmed, window_hours=24)

    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM news_asset_status WHERE asset=$1", asset)
    assert len(rows) == 1  # replaced in place, not accumulated
    assert rows[0]["status"] == "CONFIRMED"
    assert rows[0]["dominant_sentiment"] == "positive"


@pytest.mark.asyncio
async def test_get_asset_status_returns_none_when_nothing_persisted(pool):
    repo = NewsRepository(pool)
    asset = f"TEST{uuid.uuid4().hex[:6].upper()}"
    assert await repo.get_asset_status(asset) is None


@pytest.mark.asyncio
async def test_get_asset_status_returns_the_latest_upserted_verdict(pool):
    from aegis.news.conflict import AssetNewsStatus

    repo = NewsRepository(pool)
    asset = f"TEST{uuid.uuid4().hex[:6].upper()}"

    conflict = AssetNewsStatus(asset=asset, status="NEWS_CONFLICT", distinct_sources=2, item_count=2,
                                dominant_sentiment=None)
    await repo.upsert_asset_status(conflict, window_hours=24)
    fetched = await repo.get_asset_status(asset)
    assert fetched.status == "NEWS_CONFLICT"

    # upsert_asset_status replaces in place (asset is the primary key) - the
    # getter must reflect the newest verdict, not a stale row.
    confirmed = AssetNewsStatus(asset=asset, status="CONFIRMED", distinct_sources=3, item_count=4,
                                 dominant_sentiment="negative")
    await repo.upsert_asset_status(confirmed, window_hours=24)
    fetched = await repo.get_asset_status(asset)
    assert fetched.status == "CONFIRMED"
    assert fetched.distinct_sources == 3
    assert fetched.dominant_sentiment == "negative"


@pytest.mark.asyncio
async def test_upsert_asset_status_also_appends_to_history(pool):
    from aegis.news.conflict import AssetNewsStatus

    repo = NewsRepository(pool)
    asset = f"TEST{uuid.uuid4().hex[:6].upper()}"

    await repo.upsert_asset_status(
        AssetNewsStatus(asset=asset, status="NEWS_CONFLICT", distinct_sources=2, item_count=2,
                         dominant_sentiment=None),
        window_hours=24,
    )
    await repo.upsert_asset_status(
        AssetNewsStatus(asset=asset, status="CONFIRMED", distinct_sources=3, item_count=4,
                         dominant_sentiment="positive"),
        window_hours=24,
    )

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT status, dominant_sentiment FROM news_asset_status_history "
            "WHERE asset = $1 ORDER BY computed_at", asset,
        )
    # Both calls appended (current-state table only keeps the latest, but
    # history must keep both - that's the entire point of this table).
    assert [dict(r) for r in rows] == [
        {"status": "NEWS_CONFLICT", "dominant_sentiment": None},
        {"status": "CONFIRMED", "dominant_sentiment": "positive"},
    ]


@pytest.mark.asyncio
async def test_fetch_status_history_returns_transitions_in_the_window_plus_the_one_just_before(pool):
    from aegis.news.conflict import AssetNewsStatus

    repo = NewsRepository(pool)
    asset = f"TEST{uuid.uuid4().hex[:6].upper()}"

    await repo.upsert_asset_status(
        AssetNewsStatus(asset=asset, status="NEWS_CONFLICT", distinct_sources=2, item_count=2,
                         dominant_sentiment=None),
        window_hours=24,
    )
    await repo.upsert_asset_status(
        AssetNewsStatus(asset=asset, status="CONFIRMED", distinct_sources=3, item_count=3,
                         dominant_sentiment="positive"),
        window_hours=24,
    )

    # The boundary must come from the DB's own clock, not the test
    # process's - comparing a client-captured `datetime.now(UTC)` against
    # server-generated `computed_at` values is flaky under any client/server
    # clock skew, however small (found live: this exact test failed
    # intermittently for exactly that reason before this fix).
    async with pool.acquire() as conn:
        confirmed_computed_at = await conn.fetchval(
            "SELECT computed_at FROM news_asset_status_history WHERE asset = $1 AND status = 'CONFIRMED'", asset,
        )

    # A window starting exactly AT the CONFIRMED row's own timestamp must
    # still include the NEWS_CONFLICT row that came before it (the "one row
    # just before start" carry-in), so a replay beginning mid-status doesn't
    # wrongly see no status for its first bars.
    history = await repo.fetch_status_history(
        asset, start=confirmed_computed_at, end=confirmed_computed_at + timedelta(minutes=1),
    )
    statuses = [s.status for _, s in history]
    assert "NEWS_CONFLICT" in statuses
    assert "CONFIRMED" in statuses


@pytest.mark.asyncio
async def test_fetch_status_history_returns_empty_for_unknown_asset(pool):
    repo = NewsRepository(pool)
    asset = f"TEST{uuid.uuid4().hex[:6].upper()}"
    history = await repo.fetch_status_history(
        asset, start=datetime.now(UTC) - timedelta(days=1), end=datetime.now(UTC),
    )
    assert history == []


@pytest.mark.asyncio
async def test_fetch_all_asset_statuses_includes_a_freshly_upserted_asset(pool):
    from aegis.news.conflict import AssetNewsStatus

    repo = NewsRepository(pool)
    asset = f"TEST{uuid.uuid4().hex[:6].upper()}"
    await repo.upsert_asset_status(
        AssetNewsStatus(asset=asset, status="CONFIRMED", distinct_sources=2, item_count=2,
                         dominant_sentiment="positive"),
        window_hours=24,
    )

    statuses = await repo.fetch_all_asset_statuses()
    by_asset = {s["asset"]: s for s in statuses}
    assert asset in by_asset
    assert by_asset[asset]["status"] == "CONFIRMED"
    assert by_asset[asset]["dominant_sentiment"] == "positive"
