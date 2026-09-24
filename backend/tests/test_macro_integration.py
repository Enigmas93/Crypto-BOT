"""Integration tests for MacroRepository against a real TimescaleDB.
Skipped automatically if the database is unreachable (see conftest.py).
"""
from __future__ import annotations

import uuid
from datetime import date

import pytest

from aegis.db.macro_repository import MacroRepository
from aegis.macro.series_config import MacroSeriesConfig
from aegis.macro.service import MacroSnapshot
from aegis.macro.models import MacroObservation


def _series_id() -> str:
    return f"TEST{uuid.uuid4().hex[:8].upper()}"


def _config(series_id: str) -> MacroSeriesConfig:
    return MacroSeriesConfig(series_id=series_id, name="Test Series", description="integration test",
                              frequency="monthly", source="fred", importance="medium", transformation="level")


@pytest.mark.asyncio
async def test_upsert_series_registry_inserts_then_updates(pool):
    repo = MacroRepository(pool)
    series_id = _series_id()

    await repo.upsert_series_registry([_config(series_id)])
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM macro_series WHERE series_id=$1", series_id)
    assert row["importance"] == "medium"

    updated = _config(series_id)
    updated.importance = "high"
    await repo.upsert_series_registry([updated])
    async with pool.acquire() as conn:
        row2 = await conn.fetchrow("SELECT * FROM macro_series WHERE series_id=$1", series_id)
    assert row2["importance"] == "high"


@pytest.mark.asyncio
async def test_insert_observations_upserts_revised_values(pool):
    repo = MacroRepository(pool)
    series_id = _series_id()
    await repo.upsert_series_registry([_config(series_id)])

    obs = MacroObservation(series_id=series_id, date=date(2026, 1, 1), value=100.0)
    await repo.insert_observations([obs])

    df1 = await repo.fetch_observations(series_id)
    assert df1["value"].iloc[0] == pytest.approx(100.0)

    # FRED revises the same date with a restated value - must overwrite, not duplicate
    revised = MacroObservation(series_id=series_id, date=date(2026, 1, 1), value=101.5)
    await repo.insert_observations([revised])

    df2 = await repo.fetch_observations(series_id)
    assert len(df2) == 1
    assert df2["value"].iloc[0] == pytest.approx(101.5)


@pytest.mark.asyncio
async def test_fetch_observations_returns_ascending(pool):
    repo = MacroRepository(pool)
    series_id = _series_id()
    await repo.upsert_series_registry([_config(series_id)])
    observations = [
        MacroObservation(series_id=series_id, date=date(2026, 1, 1), value=1.0),
        MacroObservation(series_id=series_id, date=date(2026, 3, 1), value=3.0),
        MacroObservation(series_id=series_id, date=date(2026, 2, 1), value=2.0),
    ]
    await repo.insert_observations(observations)

    df = await repo.fetch_observations(series_id)

    assert df["value"].tolist() == [1.0, 2.0, 3.0]
    assert df["date"].is_monotonic_increasing


@pytest.mark.asyncio
async def test_upsert_snapshot_replaces_the_single_row_per_series(pool):
    repo = MacroRepository(pool)
    series_id = _series_id()
    await repo.upsert_series_registry([_config(series_id)])

    first = MacroSnapshot(series_id=series_id, as_of=date(2026, 1, 1), data_points=10, quality="PARTIAL_HISTORY",
                           value=5.0, change_pct=1.0)
    await repo.upsert_snapshot(first)

    second = MacroSnapshot(series_id=series_id, as_of=date(2026, 2, 1), data_points=11, quality="OK",
                            value=5.5, change_pct=10.0, yoy_pct_change=2.0)
    await repo.upsert_snapshot(second)

    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM macro_snapshots WHERE series_id=$1", series_id)
    assert len(rows) == 1  # replaced in place, not accumulated
    assert rows[0]["value"] == pytest.approx(5.5)
    assert rows[0]["quality"] == "OK"


@pytest.mark.asyncio
async def test_fetch_all_snapshots_joins_registry_and_latest_snapshot(pool):
    repo = MacroRepository(pool)
    series_id = _series_id()
    await repo.upsert_series_registry([_config(series_id)])
    snapshot = MacroSnapshot(series_id=series_id, as_of=date(2026, 3, 1), data_points=12, quality="OK",
                              value=4.25, change_pct=0.5, yoy_pct_change=1.1, zscore_vs_trailing=0.3)
    await repo.upsert_snapshot(snapshot)

    rows = await repo.fetch_all_snapshots()
    row = next(r for r in rows if r["series_id"] == series_id)
    assert row["name"] == "Test Series"
    assert row["value"] == pytest.approx(4.25)
    assert row["quality"] == "OK"


@pytest.mark.asyncio
async def test_fetch_all_snapshots_includes_series_with_no_snapshot_yet(pool):
    repo = MacroRepository(pool)
    series_id = _series_id()
    await repo.upsert_series_registry([_config(series_id)])  # no upsert_snapshot call

    rows = await repo.fetch_all_snapshots()
    row = next(r for r in rows if r["series_id"] == series_id)
    assert row["value"] is None  # LEFT JOIN - registered but not yet computed, not fabricated


@pytest.mark.asyncio
async def test_upsert_snapshot_skips_no_data(pool):
    repo = MacroRepository(pool)
    series_id = _series_id()
    snapshot = MacroSnapshot(series_id=series_id, as_of=None, data_points=0, quality="NO_DATA")

    await repo.upsert_snapshot(snapshot)  # must not raise

    async with pool.acquire() as conn:
        count = await conn.fetchval("SELECT count(*) FROM macro_snapshots WHERE series_id=$1", series_id)
    assert count == 0
