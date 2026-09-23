"""Integration tests for LiquidationRepository against a real TimescaleDB.
Skipped automatically if the database is unreachable (see conftest.py).
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from aegis.db.liquidation_repository import LiquidationRepository
from aegis.providers.binance.models import LiquidationEvent


def _symbol() -> str:
    return f"TEST{uuid.uuid4().hex[:8].upper()}"


def _event(symbol: str, minute_offset: int, side: str, notional_qty: float) -> LiquidationEvent:
    base = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=minute_offset)
    return LiquidationEvent(
        symbol=symbol, side=side, quantity=notional_qty, price=100.0, average_price=100.0,
        order_status="FILLED", event_time_ms=int(base.timestamp() * 1000),
    )


@pytest.mark.asyncio
async def test_insert_events_dedups_exact_repeats(pool):
    repo = LiquidationRepository(pool)
    symbol = _symbol()
    event = _event(symbol, 0, "SELL", 1.0)

    await repo.insert_events([event])
    await repo.insert_events([event])  # exact repeat

    async with pool.acquire() as conn:
        count = await conn.fetchval("SELECT count(*) FROM liquidations WHERE symbol=$1", symbol)
    assert count == 1


@pytest.mark.asyncio
async def test_fetch_windowed_stats_buckets_and_separates_by_side(pool):
    repo = LiquidationRepository(pool)
    symbol = _symbol()
    # Anchor to the start of the current 60s bucket (not raw `now()`) so
    # all three events land in the same time_bucket() regardless of how
    # close `now()` happens to be to a minute boundary when the test runs.
    bucket_start = datetime.now(UTC).replace(second=0, microsecond=0)
    events = [
        LiquidationEvent(symbol=symbol, side="SELL", quantity=1.0, price=100.0, average_price=100.0,
                          order_status="FILLED", event_time_ms=int(bucket_start.timestamp() * 1000) + 1000),
        LiquidationEvent(symbol=symbol, side="SELL", quantity=2.0, price=100.0, average_price=100.0,
                          order_status="FILLED", event_time_ms=int(bucket_start.timestamp() * 1000) + 2000),
        LiquidationEvent(symbol=symbol, side="BUY", quantity=0.5, price=100.0, average_price=100.0,
                          order_status="FILLED", event_time_ms=int(bucket_start.timestamp() * 1000) + 3000),
    ]
    await repo.insert_events(events)

    df = await repo.fetch_windowed_stats(symbol, window_seconds=60, num_buckets=5)

    assert len(df) >= 1
    last = df.iloc[-1]
    assert last["long_notional"] == pytest.approx(300.0)  # (1.0 + 2.0) * 100
    assert last["short_notional"] == pytest.approx(50.0)  # 0.5 * 100
    assert last["long_count"] == 2
    assert last["short_count"] == 1


@pytest.mark.asyncio
async def test_fetch_windowed_stats_empty_for_unknown_symbol(pool):
    repo = LiquidationRepository(pool)
    df = await repo.fetch_windowed_stats(_symbol(), window_seconds=300, num_buckets=20)
    assert df.empty
    assert list(df.columns) == ["bucket_time", "long_notional", "short_notional", "long_count", "short_count"]
