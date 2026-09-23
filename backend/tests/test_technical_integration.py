"""Integration tests for the Phase 3 read path (CandleRepository) and write
path (FeatureRepository) against a real TimescaleDB. Skipped automatically
if the database is unreachable - see conftest.py's `pool` fixture.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from aegis.db.candle_repository import CandleRepository
from aegis.db.feature_repository import FeatureRepository
from aegis.db.market_repository import MarketRepository
from aegis.providers.binance.models import Kline
from aegis.technical.service import TechnicalSnapshot


def _symbol() -> str:
    return f"TEST{uuid.uuid4().hex[:8].upper()}"


def _kline_at(symbol: str, minute_offset: int, close: float, is_closed: bool = True) -> Kline:
    base = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=minute_offset)
    open_ms = int(base.timestamp() * 1000)
    return Kline(
        symbol=symbol, interval="1m", open_time_ms=open_ms, close_time_ms=open_ms + 59999,
        open=close, high=close + 1, low=close - 1, close=close, volume=10, quote_volume=1000,
        trades=5, taker_buy_base_volume=5, taker_buy_quote_volume=500, is_closed=is_closed,
    )


@pytest.mark.asyncio
async def test_candle_repository_returns_only_closed_candles_ascending(pool):
    symbol = _symbol()
    repo = MarketRepository(pool)
    klines = [_kline_at(symbol, i, 100 + i) for i in range(5)]
    klines[-1] = _kline_at(symbol, 4, 104, is_closed=False)  # still forming
    await repo.upsert_candles(klines)

    candles = CandleRepository(pool)
    df = await candles.fetch_ohlcv(symbol, "1m", limit=500, closed_only=True)

    assert len(df) == 4  # the forming candle is excluded
    assert df["close"].tolist() == [100.0, 101.0, 102.0, 103.0]
    assert df["open_time"].is_monotonic_increasing


@pytest.mark.asyncio
async def test_candle_repository_empty_for_unknown_symbol(pool):
    candles = CandleRepository(pool)
    df = await candles.fetch_ohlcv(_symbol(), "1m")
    assert df.empty


@pytest.mark.asyncio
async def test_feature_repository_merges_features_on_conflict(pool):
    symbol = _symbol()
    repo = FeatureRepository(pool)
    as_of = datetime(2026, 1, 1, 1, 0, tzinfo=UTC)

    first = TechnicalSnapshot(symbol=symbol, interval="1m", as_of=as_of, data_points=300, quality="OK",
                               close=100.0, rsi_14=55.0)
    await repo.upsert_snapshot(first)

    # simulate a second engine (e.g. a later phase) writing different keys
    # for the SAME bar - it must merge, not clobber `close`/`rsi_14`.
    second = TechnicalSnapshot(symbol=symbol, interval="1m", as_of=as_of, data_points=300, quality="OK",
                                atr_14=2.5)
    await repo.upsert_snapshot(second)

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT features, quality FROM market_features WHERE symbol=$1 AND interval=$2 AND as_of=$3",
            symbol, "1m", as_of,
        )
    import json
    features = json.loads(row["features"]) if isinstance(row["features"], str) else row["features"]
    assert features["close"] == 100.0
    assert features["rsi_14"] == 55.0
    assert features["atr_14"] == 2.5


@pytest.mark.asyncio
async def test_feature_repository_skips_no_data_snapshot(pool):
    symbol = _symbol()
    repo = FeatureRepository(pool)
    snapshot = TechnicalSnapshot(symbol=symbol, interval="1m", as_of=None, data_points=0, quality="NO_DATA")

    await repo.upsert_snapshot(snapshot)  # must not raise, must not insert a row

    async with pool.acquire() as conn:
        count = await conn.fetchval("SELECT count(*) FROM market_features WHERE symbol=$1", symbol)
    assert count == 0
