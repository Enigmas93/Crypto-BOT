"""Integration tests for DerivativesRepository against a real TimescaleDB.
Skipped automatically if the database is unreachable (see conftest.py).
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from aegis.db.derivatives_repository import DerivativesRepository
from aegis.db.feature_repository import FeatureRepository
from aegis.derivatives.service import DerivativesSnapshot
from aegis.providers.binance.models import LongShortRatioPoint, OpenInterestHistPoint
from aegis.technical.service import TechnicalSnapshot


def _symbol() -> str:
    return f"TEST{uuid.uuid4().hex[:8].upper()}"


@pytest.mark.asyncio
async def test_insert_open_interest_dedups_on_conflict(pool):
    repo = DerivativesRepository(pool)
    symbol = _symbol()
    point = OpenInterestHistPoint(symbol=symbol, period="5m", sum_open_interest=100.0,
                                   sum_open_interest_value=5_000_000.0, timestamp_ms=1_700_000_000_000)

    await repo.insert_open_interest([point])
    await repo.insert_open_interest([point])  # exact repeat - re-poll of the same bucket

    async with pool.acquire() as conn:
        count = await conn.fetchval("SELECT count(*) FROM open_interest WHERE symbol=$1", symbol)
    assert count == 1


@pytest.mark.asyncio
async def test_fetch_open_interest_series_returns_ascending(pool):
    repo = DerivativesRepository(pool)
    symbol = _symbol()
    points = [
        OpenInterestHistPoint(symbol=symbol, period="5m", sum_open_interest=v,
                               sum_open_interest_value=v * 1000, timestamp_ms=1_700_000_000_000 + i * 300_000)
        for i, v in enumerate([100.0, 110.0, 120.0])
    ]
    await repo.insert_open_interest(points)

    df = await repo.fetch_open_interest_series(symbol, "5m", limit=10)

    assert df["sum_open_interest"].tolist() == [100.0, 110.0, 120.0]
    assert df["time"].is_monotonic_increasing


@pytest.mark.asyncio
async def test_insert_long_short_ratios_dedups_and_separates_by_type(pool):
    repo = DerivativesRepository(pool)
    symbol = _symbol()
    global_point = LongShortRatioPoint(symbol=symbol, ratio_type="GLOBAL_ACCOUNT", period="5m",
                                        long_short_ratio=1.5, long_account=0.6, short_account=0.4,
                                        timestamp_ms=1_700_000_000_000)
    top_point = LongShortRatioPoint(symbol=symbol, ratio_type="TOP_ACCOUNT", period="5m",
                                     long_short_ratio=2.0, long_account=0.66, short_account=0.34,
                                     timestamp_ms=1_700_000_000_000)

    await repo.insert_long_short_ratios([global_point, top_point])
    await repo.insert_long_short_ratios([global_point])  # repeat

    global_df = await repo.fetch_long_short_series(symbol, "GLOBAL_ACCOUNT", "5m")
    top_df = await repo.fetch_long_short_series(symbol, "TOP_ACCOUNT", "5m")

    assert len(global_df) == 1
    assert global_df["long_short_ratio"].iloc[0] == pytest.approx(1.5)
    assert len(top_df) == 1
    assert top_df["long_short_ratio"].iloc[0] == pytest.approx(2.0)


@pytest.mark.asyncio
async def test_fetch_funding_series_reads_back_phase1_data(pool):
    """funding_rates has been populated by the live collector since Phase 1
    - this just confirms the read path DerivativesEngine relies on works."""
    repo = DerivativesRepository(pool)
    df = await repo.fetch_funding_series("BTCUSDT", limit=5)
    # not asserting non-empty: depends on whether the collector has run in
    # this environment - only that the read path itself doesn't error and
    # returns the expected shape either way.
    assert list(df.columns) == ["event_time", "mark_price", "index_price", "funding_rate"]


@pytest.mark.asyncio
async def test_derivatives_snapshot_merges_into_same_market_features_row_as_technical(pool):
    """The whole point of the shared `market_features` design: a
    DerivativesSnapshot (which has `.period`, not `.interval`) must write
    into the exact same row a TechnicalSnapshot already wrote, keyed on the
    same (symbol, timeframe, as_of)."""
    symbol = _symbol()
    feature_repo = FeatureRepository(pool)
    as_of = datetime(2026, 1, 1, 2, 0, tzinfo=UTC)

    technical = TechnicalSnapshot(symbol=symbol, interval="5m", as_of=as_of, data_points=300,
                                   quality="OK", close=50000.0, rsi_14=60.0)
    await feature_repo.upsert_snapshot(technical)

    derivatives = DerivativesSnapshot(symbol=symbol, period="5m", as_of=as_of, data_points=30,
                                       quality="OK", open_interest=12345.0, funding_rate=0.0001)
    await feature_repo.upsert_snapshot(derivatives)

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT features FROM market_features WHERE symbol=$1 AND interval=$2 AND as_of=$3",
            symbol, "5m", as_of,
        )
    import json
    features = json.loads(row["features"]) if isinstance(row["features"], str) else row["features"]
    assert features["close"] == 50000.0       # from the technical snapshot
    assert features["rsi_14"] == 60.0          # from the technical snapshot
    assert features["open_interest"] == 12345.0  # from the derivatives snapshot
    assert features["funding_rate"] == 0.0001    # from the derivatives snapshot
