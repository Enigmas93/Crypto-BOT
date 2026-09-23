#!/usr/bin/env python
"""Manual smoke-test entry point for the Market Collector.

Usage (from backend/, with the venv active):
    python scripts/run_collector.py

Connects to Binance Futures TESTNET by default (see .env / BINANCE_TESTNET).
This only reads public market data - no API key is required or used.

If DATABASE_URL is set (the default, matching docker-compose.yml), events
are batched and persisted via PersistenceWriter; otherwise the collector
falls back to logging every event, exactly like Phase 1. Press Ctrl+C to
stop - the writer flushes whatever is buffered before exiting.
"""
from __future__ import annotations

import asyncio
import dataclasses
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aegis.collector.market_collector import MarketCollector  # noqa: E402
from aegis.config import get_settings  # noqa: E402
from aegis.db.engine import close_pool, create_pool  # noqa: E402
from aegis.db.market_repository import MarketRepository  # noqa: E402
from aegis.db.persistence_writer import PersistenceWriter  # noqa: E402
from aegis.logging_utils import configure_logging, get_logger, log_event  # noqa: E402

_LOG = get_logger("scripts.run_collector")


def _log_event(event: object) -> None:
    log_event(
        _LOG, "market_event",
        event_class=type(event).__name__, payload=dataclasses.asdict(event),
    )


async def _report_stats(writer: PersistenceWriter, interval: float = 10.0) -> None:
    while True:
        await asyncio.sleep(interval)
        log_event(
            _LOG, "writer_stats",
            events_written=writer.events_written,
            events_dropped=writer.events_dropped,
            flush_count=writer.flush_count,
        )


async def _main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    log_event(
        _LOG, "startup",
        testnet=settings.binance_testnet, symbols=settings.symbols, intervals=settings.intervals,
        persistence=bool(settings.database_url),
    )

    pool = None
    writer: PersistenceWriter | None = None
    stats_task: asyncio.Task | None = None
    on_event = _log_event

    if settings.database_url:
        pool = await create_pool(settings)
        repo = MarketRepository(pool)
        writer = PersistenceWriter(
            repository=repo,
            batch_size=settings.db_writer_batch_size,
            flush_interval=settings.db_writer_flush_interval,
            queue_max_size=settings.db_writer_queue_max_size,
        )
        await writer.start()
        stats_task = asyncio.create_task(_report_stats(writer))
        on_event = writer.on_event

    collector = MarketCollector(settings=settings, on_event=on_event)
    try:
        await collector.run()
    finally:
        await collector.aclose()
        if stats_task is not None:
            stats_task.cancel()
        if writer is not None:
            await writer.stop()
            log_event(
                _LOG, "writer_final_stats",
                events_written=writer.events_written, events_dropped=writer.events_dropped,
            )
        if pool is not None:
            await close_pool(pool)


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
