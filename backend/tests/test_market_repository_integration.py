"""Integration tests against a real TimescaleDB instance.

Requires `docker compose up -d` (repo root) and `alembic upgrade head` (see
backend/README.md) to have been run first. Skipped automatically - not
failed - if the database is unreachable, so the rest of the suite stays
runnable offline.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from aegis.db.market_repository import MarketRepository
from aegis.providers.binance.models import AggTrade, BookTicker, Kline, MarkPrice


def _symbol() -> str:
    return f"TEST{uuid.uuid4().hex[:8].upper()}"


@pytest.mark.asyncio
async def test_upsert_assets_inserts_then_updates_last_seen(pool):
    repo = MarketRepository(pool)
    symbol = _symbol()

    await repo.upsert_assets({symbol})
    async with pool.acquire() as conn:
        row1 = await conn.fetchrow("SELECT * FROM assets WHERE symbol = $1", symbol)
    assert row1["status"] == "ACTIVE"
    first_seen = row1["first_seen_at"]

    await repo.upsert_assets({symbol})
    async with pool.acquire() as conn:
        row2 = await conn.fetchrow("SELECT * FROM assets WHERE symbol = $1", symbol)
    assert row2["first_seen_at"] == first_seen  # unchanged
    assert row2["last_seen_at"] >= row1["last_seen_at"]


@pytest.mark.asyncio
async def test_upsert_candles_updates_in_place_on_conflict(pool):
    repo = MarketRepository(pool)
    symbol = _symbol()
    open_time_ms = int(datetime.now(tz=UTC).timestamp() * 1000)

    forming = Kline(
        symbol=symbol, interval="1m", open_time_ms=open_time_ms, close_time_ms=open_time_ms + 59999,
        open=100.0, high=101.0, low=99.0, close=100.5, volume=10, quote_volume=1000,
        trades=5, taker_buy_base_volume=5, taker_buy_quote_volume=500, is_closed=False,
    )
    await repo.upsert_candles([forming])

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM candles WHERE symbol = $1 AND interval = $2", symbol, "1m"
        )
    assert row["is_closed"] is False
    assert row["close"] == 100.5

    closed = Kline(
        symbol=symbol, interval="1m", open_time_ms=open_time_ms, close_time_ms=open_time_ms + 59999,
        open=100.0, high=102.0, low=99.0, close=101.5, volume=20, quote_volume=2000,
        trades=9, taker_buy_base_volume=9, taker_buy_quote_volume=900, is_closed=True,
    )
    await repo.upsert_candles([closed])

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM candles WHERE symbol = $1 AND interval = $2", symbol, "1m"
        )
    assert len(rows) == 1  # updated in place, not a second row
    assert rows[0]["is_closed"] is True
    assert rows[0]["close"] == 101.5
    assert rows[0]["trades"] == 9


@pytest.mark.asyncio
async def test_insert_trades_dedups_on_agg_trade_id(pool):
    repo = MarketRepository(pool)
    symbol = _symbol()
    now_ms = int(datetime.now(tz=UTC).timestamp() * 1000)
    trade = AggTrade(symbol=symbol, agg_trade_id=1, price=50000.0, quantity=0.01,
                      trade_time_ms=now_ms, is_buyer_maker=True)

    await repo.insert_trades([trade])
    await repo.insert_trades([trade])  # exact repeat, e.g. REST gap-fill overlap

    async with pool.acquire() as conn:
        count = await conn.fetchval("SELECT count(*) FROM trades WHERE symbol = $1", symbol)
    assert count == 1


@pytest.mark.asyncio
async def test_insert_funding_rates_and_book_ticker(pool):
    repo = MarketRepository(pool)
    symbol = _symbol()
    now_ms = int(datetime.now(tz=UTC).timestamp() * 1000)

    mark = MarkPrice(symbol=symbol, mark_price=50000.0, index_price=50010.0,
                      estimated_settle_price=50005.0, funding_rate=0.0001,
                      next_funding_time_ms=now_ms + 3600_000, event_time_ms=now_ms)
    ticker = BookTicker(symbol=symbol, best_bid_price=49999.0, best_bid_qty=1.0,
                         best_ask_price=50001.0, best_ask_qty=1.0, event_time_ms=now_ms)

    await repo.insert_funding_rates([mark])
    await repo.insert_book_ticker([ticker])

    async with pool.acquire() as conn:
        funding_row = await conn.fetchrow("SELECT * FROM funding_rates WHERE symbol = $1", symbol)
        ticker_row = await conn.fetchrow("SELECT * FROM book_ticker WHERE symbol = $1", symbol)

    assert funding_row["funding_rate"] == pytest.approx(0.0001)
    assert ticker_row["best_bid_price"] == pytest.approx(49999.0)
