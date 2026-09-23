"""Persistence for the four market-data streams the Phase 1 collector
produces. One method per stream, each a single batched `executemany` inside
a transaction - never one round-trip per event.

Symbols are self-registered into `assets` the first time they're seen, so
this repository is the seed of the asset registry future phases (Asset
Scanner, Portfolio Engine) will build on.
"""
from __future__ import annotations

from datetime import UTC, datetime

import asyncpg

from aegis.providers.binance.models import AggTrade, BookTicker, Kline, MarkPrice


def _ms_to_dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000.0, tz=UTC)


class MarketRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def upsert_assets(self, symbols: set[str]) -> None:
        if not symbols:
            return
        rows = [(s,) for s in symbols]
        async with self._pool.acquire() as conn:
            await conn.executemany(
                """
                INSERT INTO assets (symbol, first_seen_at, last_seen_at)
                VALUES ($1, now(), now())
                ON CONFLICT (symbol) DO UPDATE SET last_seen_at = now()
                """,
                rows,
            )

    async def upsert_candles(self, klines: list[Kline]) -> None:
        if not klines:
            return
        rows = [
            (
                k.symbol, k.interval, _ms_to_dt(k.open_time_ms), _ms_to_dt(k.close_time_ms),
                k.open, k.high, k.low, k.close, k.volume, k.quote_volume, k.trades,
                k.taker_buy_base_volume, k.taker_buy_quote_volume, k.is_closed, k.source,
            )
            for k in klines
        ]
        async with self._pool.acquire() as conn:
            await conn.executemany(
                """
                INSERT INTO candles (
                    symbol, interval, open_time, close_time, open, high, low, close,
                    volume, quote_volume, trades, taker_buy_base_volume,
                    taker_buy_quote_volume, is_closed, source
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)
                ON CONFLICT (symbol, interval, open_time) DO UPDATE SET
                    close_time = EXCLUDED.close_time,
                    high = EXCLUDED.high,
                    low = EXCLUDED.low,
                    close = EXCLUDED.close,
                    volume = EXCLUDED.volume,
                    quote_volume = EXCLUDED.quote_volume,
                    trades = EXCLUDED.trades,
                    taker_buy_base_volume = EXCLUDED.taker_buy_base_volume,
                    taker_buy_quote_volume = EXCLUDED.taker_buy_quote_volume,
                    is_closed = EXCLUDED.is_closed
                """,
                rows,
            )

    async def insert_trades(self, trades: list[AggTrade]) -> None:
        if not trades:
            return
        rows = [
            (t.symbol, t.agg_trade_id, t.price, t.quantity, _ms_to_dt(t.trade_time_ms),
             t.is_buyer_maker, t.source)
            for t in trades
        ]
        async with self._pool.acquire() as conn:
            await conn.executemany(
                """
                INSERT INTO trades (symbol, agg_trade_id, price, quantity, trade_time,
                                     is_buyer_maker, source)
                VALUES ($1,$2,$3,$4,$5,$6,$7)
                ON CONFLICT (symbol, agg_trade_id, trade_time) DO NOTHING
                """,
                rows,
            )

    async def insert_funding_rates(self, mark_prices: list[MarkPrice]) -> None:
        if not mark_prices:
            return
        rows = [
            (m.symbol, _ms_to_dt(m.event_time_ms), m.mark_price, m.index_price,
             m.estimated_settle_price, m.funding_rate, _ms_to_dt(m.next_funding_time_ms), m.source)
            for m in mark_prices
        ]
        async with self._pool.acquire() as conn:
            await conn.executemany(
                """
                INSERT INTO funding_rates (symbol, event_time, mark_price, index_price,
                                            estimated_settle_price, funding_rate,
                                            next_funding_time, source)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
                ON CONFLICT (symbol, event_time) DO NOTHING
                """,
                rows,
            )

    async def insert_book_ticker(self, tickers: list[BookTicker]) -> None:
        if not tickers:
            return
        rows = [
            (b.symbol, _ms_to_dt(b.event_time_ms), b.best_bid_price, b.best_bid_qty,
             b.best_ask_price, b.best_ask_qty, b.source)
            for b in tickers
        ]
        async with self._pool.acquire() as conn:
            await conn.executemany(
                """
                INSERT INTO book_ticker (symbol, event_time, best_bid_price, best_bid_qty,
                                          best_ask_price, best_ask_qty, source)
                VALUES ($1,$2,$3,$4,$5,$6,$7)
                ON CONFLICT (symbol, event_time) DO NOTHING
                """,
                rows,
            )
