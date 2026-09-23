#!/usr/bin/env python
"""Runs the Phase 4c Order Book Engine forever: subscribes to the partial
book depth stream for every configured symbol, and periodically computes
and merges a derived feature snapshot (spread, microprice, imbalance, book
pressure) into `market_features`. Raw depth ticks are never persisted -
only the periodic derived snapshot (see service.py's docstring for why).

Usage (from backend/, venv active):
    python scripts/run_orderbook_engine.py

Ctrl+C to stop.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aegis.config import get_settings  # noqa: E402
from aegis.db.engine import close_pool, create_pool  # noqa: E402
from aegis.db.feature_repository import FeatureRepository  # noqa: E402
from aegis.logging_utils import configure_logging, get_logger, log_event  # noqa: E402
from aegis.orderbook.service import OrderBookEngine  # noqa: E402

_LOG = get_logger("scripts.run_orderbook_engine")


async def _main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    log_event(
        _LOG, "startup",
        symbols=settings.symbols, depth_levels=settings.orderbook_depth_levels,
        snapshot_interval=settings.orderbook_snapshot_interval_seconds,
    )

    pool = await create_pool(settings)
    engine = OrderBookEngine(feature_repo=FeatureRepository(pool), settings=settings)
    try:
        await engine.run()
    finally:
        await close_pool(pool)


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
