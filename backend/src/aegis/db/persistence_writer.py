"""Turns the collector's synchronous `on_event` callback into batched,
async database writes.

Design: `on_event` (called from inside the WS dispatch loop) only ever does
a non-blocking `queue.put_nowait` - it must never block message processing
on a DB round-trip. A background task drains the queue, batches events by
type, and flushes them through `MarketRepository` on a size/time trigger.

If the queue fills up (DB down/slow for longer than `queue_max_size` worth
of events), new events are dropped and counted rather than blocking the
collector or growing memory without bound - this is a deliberate trade-off,
documented as a known limitation until a durable local buffer exists.
"""
from __future__ import annotations

import asyncio
from typing import Protocol

from aegis.logging_utils import get_logger, log_event
from aegis.providers.binance.models import AggTrade, BookTicker, Kline, MarkPrice

_LOG = get_logger("db.persistence_writer")

MarketEvent = Kline | AggTrade | MarkPrice | BookTicker


class SupportsMarketWrites(Protocol):
    async def upsert_assets(self, symbols: set[str]) -> None: ...
    async def upsert_candles(self, klines: list[Kline]) -> None: ...
    async def insert_trades(self, trades: list[AggTrade]) -> None: ...
    async def insert_funding_rates(self, mark_prices: list[MarkPrice]) -> None: ...
    async def insert_book_ticker(self, tickers: list[BookTicker]) -> None: ...


class PersistenceWriter:
    def __init__(
        self,
        repository: SupportsMarketWrites,
        batch_size: int = 200,
        flush_interval: float = 1.0,
        queue_max_size: int = 20_000,
    ) -> None:
        self._repo = repository
        self._batch_size = batch_size
        self._flush_interval = flush_interval
        self._queue: asyncio.Queue[MarketEvent] = asyncio.Queue(maxsize=queue_max_size)
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self.events_written = 0
        self.events_dropped = 0
        self.flush_count = 0
        self._known_symbols: set[str] = set()
        self._new_symbols: set[str] = set()

    def on_event(self, event: MarketEvent) -> None:
        """Sync callback wired as `MarketCollector`'s `on_event`."""
        symbol = getattr(event, "symbol", None)
        if symbol and symbol not in self._known_symbols:
            self._known_symbols.add(symbol)
            self._new_symbols.add(symbol)
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            self.events_dropped += 1
            if self.events_dropped % 500 == 1:
                log_event(
                    _LOG, "writer_queue_full", level=40,
                    symbol=symbol, total_dropped=self.events_dropped,
                )

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            await self._task

    async def _run(self) -> None:
        buffer: list[MarketEvent] = []
        while not self._stop_event.is_set() or not self._queue.empty() or buffer:
            if not buffer:
                try:
                    item = await asyncio.wait_for(self._queue.get(), timeout=self._flush_interval)
                    buffer.append(item)
                except TimeoutError:
                    continue
            while len(buffer) < self._batch_size:
                try:
                    buffer.append(self._queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            await self._flush(buffer)
            buffer = []

    async def _flush(self, buffer: list[MarketEvent]) -> None:
        klines = [e for e in buffer if isinstance(e, Kline)]
        trades = [e for e in buffer if isinstance(e, AggTrade)]
        marks = [e for e in buffer if isinstance(e, MarkPrice)]
        tickers = [e for e in buffer if isinstance(e, BookTicker)]

        try:
            if self._new_symbols:
                await self._repo.upsert_assets(set(self._new_symbols))
                self._new_symbols.clear()
            if klines:
                await self._repo.upsert_candles(klines)
            if trades:
                await self._repo.insert_trades(trades)
            if marks:
                await self._repo.insert_funding_rates(marks)
            if tickers:
                await self._repo.insert_book_ticker(tickers)
        except Exception as exc:  # noqa: BLE001 - a flush failure must not kill the writer task
            log_event(_LOG, "flush_failed", level=40, error=str(exc), batch_size=len(buffer))
            return

        self.events_written += len(buffer)
        self.flush_count += 1
