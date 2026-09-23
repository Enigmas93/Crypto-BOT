import asyncio

import pytest

from aegis.db.persistence_writer import PersistenceWriter
from aegis.providers.binance.models import AggTrade, BookTicker, Kline, MarkPrice


class FakeRepository:
    def __init__(self):
        self.asset_calls: list[set[str]] = []
        self.candle_batches: list[list[Kline]] = []
        self.trade_batches: list[list[AggTrade]] = []
        self.funding_batches: list[list[MarkPrice]] = []
        self.ticker_batches: list[list[BookTicker]] = []

    async def upsert_assets(self, symbols):
        self.asset_calls.append(set(symbols))

    async def upsert_candles(self, klines):
        self.candle_batches.append(list(klines))

    async def insert_trades(self, trades):
        self.trade_batches.append(list(trades))

    async def insert_funding_rates(self, mark_prices):
        self.funding_batches.append(list(mark_prices))

    async def insert_book_ticker(self, tickers):
        self.ticker_batches.append(list(tickers))


def _kline(symbol="BTCUSDT") -> Kline:
    return Kline(
        symbol=symbol, interval="1m", open_time_ms=0, close_time_ms=1,
        open=1, high=2, low=0.5, close=1.5, volume=10, quote_volume=15,
        trades=3, taker_buy_base_volume=5, taker_buy_quote_volume=7.5, is_closed=True,
    )


@pytest.mark.asyncio
async def test_flush_by_batch_size_routes_events_by_type():
    repo = FakeRepository()
    writer = PersistenceWriter(repository=repo, batch_size=2, flush_interval=5.0)
    await writer.start()

    writer.on_event(_kline())
    writer.on_event(_kline())  # hits batch_size=2 -> triggers a flush

    await asyncio.sleep(0.2)
    await writer.stop()

    assert writer.events_written == 2
    assert sum(len(b) for b in repo.candle_batches) == 2
    assert repo.asset_calls[0] == {"BTCUSDT"}


@pytest.mark.asyncio
async def test_flush_by_time_interval_even_below_batch_size():
    repo = FakeRepository()
    writer = PersistenceWriter(repository=repo, batch_size=100, flush_interval=0.05)
    await writer.start()

    writer.on_event(_kline())

    await asyncio.sleep(0.25)
    await writer.stop()

    assert writer.events_written == 1
    assert len(repo.candle_batches) == 1


@pytest.mark.asyncio
async def test_mixed_event_types_split_into_correct_repository_calls():
    repo = FakeRepository()
    writer = PersistenceWriter(repository=repo, batch_size=4, flush_interval=5.0)
    await writer.start()

    writer.on_event(_kline())
    writer.on_event(AggTrade(symbol="BTCUSDT", agg_trade_id=1, price=1, quantity=1,
                              trade_time_ms=1, is_buyer_maker=False))
    writer.on_event(MarkPrice(symbol="BTCUSDT", mark_price=1, index_price=1,
                               estimated_settle_price=1, funding_rate=0.0001,
                               next_funding_time_ms=1, event_time_ms=1))
    writer.on_event(BookTicker(symbol="BTCUSDT", best_bid_price=1, best_bid_qty=1,
                                best_ask_price=1.1, best_ask_qty=1, event_time_ms=1))

    await asyncio.sleep(0.2)
    await writer.stop()

    assert writer.events_written == 4
    assert len(repo.candle_batches[0]) == 1
    assert len(repo.trade_batches[0]) == 1
    assert len(repo.funding_batches[0]) == 1
    assert len(repo.ticker_batches[0]) == 1


@pytest.mark.asyncio
async def test_queue_full_drops_events_instead_of_blocking():
    repo = FakeRepository()
    # flush_interval huge + batch_size huge => nothing drains while we fill the queue
    writer = PersistenceWriter(repository=repo, batch_size=1000, flush_interval=1000, queue_max_size=3)
    await writer.start()
    await asyncio.sleep(0.05)  # let the writer task start and block on queue.get()

    for _ in range(3):
        writer.on_event(_kline())
    writer.on_event(_kline())  # 4th event: queue already at maxsize=3 -> dropped, not blocked

    await writer.stop()  # drains + flushes the 3 buffered events before returning

    assert writer.events_dropped == 1
    assert writer.events_written == 3
