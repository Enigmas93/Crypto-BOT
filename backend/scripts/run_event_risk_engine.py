#!/usr/bin/env python
"""Runs the Fase 15b Event Risk Engine forever: polls CoinMarketCal for
upcoming crypto-specific events (listings, mainnet launches, hard forks)
mentioning any of the tracked symbols' underlying assets, and persists
them (upserted, not append-only - see migration 0017's docstring).

Crypto-native events only - macro events (FOMC/CPI/NFP) have no good free
API and stay out of scope here, not silently assumed covered. See
backend/README.md "Métricas da Fase 15b" for the real, verified research
behind that gap, and aegis/providers/coinmarketcal/client.py for how this
API was verified live (the docs site is Cloudflare-blocked; the actual API
subdomain isn't).

Refuses to run without COINMARKETCAL_API_KEY configured, same policy as
FRED/BEA - logged, not a crash.

Usage (from backend/, venv active):
    python scripts/run_event_risk_engine.py

Ctrl+C to stop.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aegis.config import get_settings  # noqa: E402
from aegis.db.engine import close_pool, create_pool  # noqa: E402
from aegis.db.events_repository import EventsRepository  # noqa: E402
from aegis.events.service import EventRiskEngine  # noqa: E402
from aegis.logging_utils import configure_logging, get_logger, log_event  # noqa: E402
from aegis.providers.coinmarketcal.client import CoinMarketCalProvider, CoinMarketCalProviderError  # noqa: E402

_LOG = get_logger("scripts.run_event_risk_engine")


async def _main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)

    if not settings.coinmarketcal_api_key:
        log_event(
            _LOG, "missing_credentials", level=30,
            message="COINMARKETCAL_API_KEY not set - register for free at https://coinmarketcal.com/developer",
        )
        return

    provider = CoinMarketCalProvider(api_key=settings.coinmarketcal_api_key)
    pool = await create_pool(settings)
    repo = EventsRepository(pool)
    engine = EventRiskEngine(provider, repo)
    coin_slugs = settings.event_risk_coin_slug_list

    log_event(_LOG, "startup", coins=coin_slugs, poll_interval=settings.event_risk_poll_interval_seconds)

    try:
        while True:
            try:
                events = await engine.run_once(coin_slugs)
            except CoinMarketCalProviderError as exc:
                # A transient network/API blip must not kill the whole
                # process - same lesson as every other poller here.
                log_event(_LOG, "transient_error", level=30, error=str(exc))
            else:
                log_event(
                    _LOG, "sync_complete", count=len(events),
                    upcoming=[{"title": e.title, "displayed_date": e.displayed_date, "coins": e.coins}
                              for e in events[:10]],
                )
            await asyncio.sleep(settings.event_risk_poll_interval_seconds)
    finally:
        await provider.aclose()
        await close_pool(pool)


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
