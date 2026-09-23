#!/usr/bin/env python
"""Manual smoke test for the Phase 3 Technical Engine.

Computes and prints (and persists) a multi-timeframe snapshot for every
configured symbol, from whatever candles are already in the database.
Run the collector for a while first (Phase 1/2) so there is real history
to compute over.

Usage (from backend/, venv active):
    python scripts/run_technical_snapshot.py
"""
from __future__ import annotations

import asyncio
import dataclasses
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aegis.config import get_settings  # noqa: E402
from aegis.db.candle_repository import CandleRepository  # noqa: E402
from aegis.db.engine import close_pool, create_pool  # noqa: E402
from aegis.db.feature_repository import FeatureRepository  # noqa: E402
from aegis.logging_utils import configure_logging, get_logger, log_event  # noqa: E402
from aegis.technical.service import TechnicalAnalysisService  # noqa: E402

_LOG = get_logger("scripts.run_technical_snapshot")


async def _main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    pool = await create_pool(settings)
    try:
        candles = CandleRepository(pool)
        features = FeatureRepository(pool)
        service = TechnicalAnalysisService(candle_source=candles, intervals=settings.intervals)

        for symbol in settings.symbols:
            snapshots = await service.compute_for_symbol(symbol)
            for interval, snapshot in snapshots.items():
                log_event(
                    _LOG, "technical_snapshot",
                    symbol=symbol, interval=interval,
                    quality=snapshot.quality, data_points=snapshot.data_points,
                    payload=dataclasses.asdict(snapshot),
                )
                await features.upsert_snapshot(snapshot)
    finally:
        await close_pool(pool)


if __name__ == "__main__":
    asyncio.run(_main())
