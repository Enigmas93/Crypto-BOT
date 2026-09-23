#!/usr/bin/env python
"""Runs the Phase 4b Liquidation Engine forever: ingests the market-wide
liquidation stream, persists raw events for the configured symbols, and
periodically computes/merges a windowed snapshot (SqueezeScore included)
into `market_features`.

Usage (from backend/, venv active):
    python scripts/run_liquidation_engine.py

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
from aegis.db.liquidation_repository import LiquidationRepository  # noqa: E402
from aegis.liquidation.service import LiquidationEngine  # noqa: E402
from aegis.logging_utils import configure_logging, get_logger, log_event  # noqa: E402

_LOG = get_logger("scripts.run_liquidation_engine")


async def _main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    log_event(
        _LOG, "startup",
        symbols=settings.symbols, window_seconds=settings.liquidation_window_seconds,
        snapshot_interval=settings.liquidation_snapshot_interval_seconds,
    )

    pool = await create_pool(settings)
    engine = LiquidationEngine(
        repository=LiquidationRepository(pool),
        feature_repo=FeatureRepository(pool),
        settings=settings,
    )
    try:
        await engine.run()
    finally:
        await close_pool(pool)


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
