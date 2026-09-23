#!/usr/bin/env python
"""Verifies the signed-endpoint setup BEFORE any order is ever placed
(Phase 10 - Execution Engine).

Deliberately read-only: fetches account balance and position risk (signed,
but non-mutating) to confirm BINANCE_API_KEY/BINANCE_API_SECRET are valid,
have Futures trading permission, and are pointed at the environment
`BINANCE_TESTNET` says they should be. Never places or cancels anything -
that is scripts/run_shadow_trading.py's job, and it should only be run
after this one confirms clean.

Usage (from backend/, venv active):
    python scripts/verify_execution_setup.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aegis.config import get_settings  # noqa: E402
from aegis.logging_utils import configure_logging, get_logger, log_event  # noqa: E402
from aegis.providers.binance.rest_client import BinanceFuturesRestClient, BinanceOrderError, BinanceRestError  # noqa: E402

_LOG = get_logger("scripts.verify_execution_setup")


async def _main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)

    if not settings.binance_api_key or not settings.binance_api_secret:
        log_event(_LOG, "missing_credentials", level=40,
                   message="BINANCE_API_KEY/BINANCE_API_SECRET not set in backend/.env - nothing to verify")
        return

    log_event(
        _LOG, "verifying", testnet=settings.binance_testnet, live_trading=settings.live_trading,
        message="Read-only check - no order will be placed or cancelled",
    )

    client = BinanceFuturesRestClient(
        testnet=settings.binance_testnet, api_key=settings.binance_api_key, api_secret=settings.binance_api_secret,
    )
    try:
        balances = await client.get_account_balance()
        usdt = next((b for b in balances if b.get("asset") == "USDT"), None)
        log_event(
            _LOG, "balance_ok", asset_count=len(balances),
            usdt_balance=usdt.get("balance") if usdt else None,
            usdt_available=usdt.get("availableBalance") if usdt else None,
        )

        positions = await client.get_position_risk()
        open_positions = [p for p in positions if p.position_amt != 0]
        log_event(
            _LOG, "position_risk_ok", total_symbols=len(positions), open_positions=len(open_positions),
            open=[{"symbol": p.symbol, "amt": p.position_amt, "entry": p.entry_price} for p in open_positions],
        )

        log_event(
            _LOG, "verified", message=(
                "Credentials are valid and have the expected permissions. "
                "Safe to proceed to scripts/run_shadow_trading.py."
            ),
        )
    except (BinanceOrderError, BinanceRestError) as exc:
        log_event(
            _LOG, "verification_failed", level=40, error=str(exc),
            message="Fix this before attempting any order placement - see error above",
        )
    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(_main())
