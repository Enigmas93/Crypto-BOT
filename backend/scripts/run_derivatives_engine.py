#!/usr/bin/env python
"""Runs the Phase 4 Derivatives Engine forever: polls OI + long/short
ratios on a fixed interval, persists the raw series, computes a
DerivativesSnapshot per (symbol, period), and merges it into
`market_features`.

Usage (from backend/, venv active):
    python scripts/run_derivatives_engine.py

Ctrl+C to stop.
"""
from __future__ import annotations

import asyncio
import dataclasses
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aegis.config import get_settings  # noqa: E402
from aegis.db.candle_repository import CandleRepository  # noqa: E402
from aegis.db.derivatives_repository import DerivativesRepository  # noqa: E402
from aegis.db.engine import close_pool, create_pool  # noqa: E402
from aegis.db.feature_repository import FeatureRepository  # noqa: E402
from aegis.derivatives.service import DerivativesEngine  # noqa: E402
from aegis.logging_utils import configure_logging, get_logger, log_event  # noqa: E402
from aegis.providers.binance.rest_client import BinanceFuturesRestClient, BinanceRestError  # noqa: E402

_LOG = get_logger("scripts.run_derivatives_engine")


async def _main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    log_event(
        _LOG, "startup",
        symbols=settings.symbols, periods=settings.derivatives_periods,
        poll_interval=settings.derivatives_poll_interval_seconds,
    )

    pool = await create_pool(settings)
    rest = BinanceFuturesRestClient(testnet=settings.binance_testnet)
    engine = DerivativesEngine(
        rest_client=rest,
        derivatives_repo=DerivativesRepository(pool),
        candle_repo=CandleRepository(pool),
        feature_repo=FeatureRepository(pool),
        settings=settings,
    )

    try:
        while True:
            try:
                results = await engine.run_once()
            except BinanceRestError as exc:
                # A transient network/DNS blip must not kill the whole
                # process - same lesson learned live from Shadow/Momentum
                # Trading crashing on 2026-09-23; just retry next cycle.
                log_event(_LOG, "transient_network_error", level=30, error=str(exc))
            else:
                for key, snapshot in results.items():
                    log_event(
                        _LOG, "derivatives_snapshot",
                        key=key, quality=snapshot.quality, payload=dataclasses.asdict(snapshot),
                    )
            await asyncio.sleep(settings.derivatives_poll_interval_seconds)
    finally:
        await rest.aclose()
        await close_pool(pool)


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
