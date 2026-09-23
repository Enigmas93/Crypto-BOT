#!/usr/bin/env python
"""Runs the Phase 5 Macro Engine forever: polls FRED, BLS and BEA for every
series in macro_series.yaml, persists raw observations (upserted - macro
data gets revised after initial release), and computes/stores a snapshot
(change %, YoY %, z-score vs trailing history) per series.

FRED_API_KEY and BEA_API_KEY are required for their respective series
(free, instant registration - see links below). BLS_API_KEY is optional -
BLS works unregistered at lower limits. A series whose source has no
configured provider (missing key) is skipped (logged), not fatal - see
`MacroEngine.run_once`.
  FRED: https://fred.stlouisfed.org/docs/api/api_key.html
  BEA:  https://apps.bea.gov/API/signup/index.cfm

Usage (from backend/, venv active):
    python scripts/run_macro_engine.py

Ctrl+C to stop.
"""
from __future__ import annotations

import asyncio
import dataclasses
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aegis.config import get_settings  # noqa: E402
from aegis.db.engine import close_pool, create_pool  # noqa: E402
from aegis.db.macro_repository import MacroRepository  # noqa: E402
from aegis.logging_utils import configure_logging, get_logger, log_event  # noqa: E402
from aegis.macro.series_config import load_macro_series  # noqa: E402
from aegis.macro.service import MacroEngine  # noqa: E402
from aegis.providers.bea.client import BeaProvider  # noqa: E402
from aegis.providers.bls.client import BlsProvider  # noqa: E402
from aegis.providers.fred.client import FredProvider  # noqa: E402


_LOG = get_logger("scripts.run_macro_engine")


async def _main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)

    series_configs = load_macro_series(settings.macro_series_config_path)

    providers = {}
    if settings.fred_api_key:
        providers["fred"] = FredProvider(api_key=settings.fred_api_key)
    else:
        log_event(_LOG, "missing_fred_api_key", level=30,
                   message="FRED series will be skipped - register at https://fred.stlouisfed.org/docs/api/api_key.html")
    if settings.bea_api_key:
        providers["bea"] = BeaProvider(api_key=settings.bea_api_key, years_back=settings.bea_years_back)
    else:
        log_event(_LOG, "missing_bea_api_key", level=30,
                   message="BEA series will be skipped - register at https://apps.bea.gov/API/signup/index.cfm")
    providers["bls"] = BlsProvider(api_key=settings.bls_api_key, history_years=settings.bls_history_years)

    log_event(_LOG, "startup", series=[c.series_id for c in series_configs],
              sources=list(providers.keys()), poll_interval=settings.macro_poll_interval_seconds)

    pool = await create_pool(settings)
    engine = MacroEngine(
        providers=providers, repository=MacroRepository(pool), settings=settings, series_configs=series_configs,
    )
    try:
        while True:
            results = await engine.run_once()
            for series_id, snapshot in results.items():
                log_event(_LOG, "macro_snapshot", series_id=series_id, quality=snapshot.quality,
                           payload=dataclasses.asdict(snapshot))
            await asyncio.sleep(settings.macro_poll_interval_seconds)
    finally:
        for provider in providers.values():
            await provider.aclose()
        await close_pool(pool)


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
