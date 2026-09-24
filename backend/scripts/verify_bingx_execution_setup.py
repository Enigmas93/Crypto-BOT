#!/usr/bin/env python
"""Verifies the BingX signed-endpoint setup BEFORE any order is ever placed
(Fase 16 - execution migration). Mirrors scripts/verify_execution_setup.py.

Deliberately mostly read-only: confirms BINGX_API_KEY/BINGX_API_SECRET are
valid against the VST (testnet) environment, checks the account's position
mode (must be one-way - see BingXExecutionProvider's docstring for why) and
switches it if needed (only safe while the account is flat), and tops up
the VST virtual balance if it's too low to size a real test trade. Never
places or cancels a trading order - that is
scripts/verify_bingx_shadow_trading.py's job, and it should only run after
this one confirms clean.

Usage (from backend/, venv active):
    python scripts/verify_bingx_execution_setup.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aegis.config import get_settings  # noqa: E402
from aegis.logging_utils import configure_logging, get_logger, log_event  # noqa: E402
from aegis.providers.bingx.rest_client import BingXFuturesRestClient, BingXOrderError, BingXRestError  # noqa: E402

_LOG = get_logger("scripts.verify_bingx_execution_setup")
_MIN_USEFUL_VST_BALANCE = 100.0
_VST_TOPUP_AMOUNT = 1000


async def _main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)

    if not settings.bingx_api_key or not settings.bingx_api_secret:
        log_event(_LOG, "missing_credentials", level=40,
                   message="BINGX_API_KEY/BINGX_API_SECRET not set in backend/.env - nothing to verify")
        return
    if not settings.bingx_testnet:
        log_event(
            _LOG, "refusing_to_run", level=40,
            message="This script only ever runs against BINGX_TESTNET=true - it is meant to validate "
                    "the VST (testnet) environment, not touch a real-money account.",
        )
        return

    log_event(_LOG, "verifying", testnet=settings.bingx_testnet, message="Read-only-ish check against BingX VST")

    client = BingXFuturesRestClient(
        testnet=True, api_key=settings.bingx_api_key, api_secret=settings.bingx_api_secret,
    )
    try:
        balances = await client.get_account_balance()
        vst = next((b for b in balances if b.get("asset") == "VST"), None)
        vst_balance = float(vst.get("balance", 0)) if vst else 0.0
        log_event(_LOG, "balance_ok", asset_count=len(balances), vst_balance=vst_balance)

        if vst_balance < _MIN_USEFUL_VST_BALANCE:
            new_balance = await client.apply_vst(_VST_TOPUP_AMOUNT, increase=True)
            log_event(_LOG, "vst_topped_up", requested=_VST_TOPUP_AMOUNT, new_balance=new_balance)

        positions = await client.get_position_risk()
        open_positions = [p for p in positions if p.position_amt != 0]
        log_event(
            _LOG, "position_risk_ok", total_symbols=len(positions), open_positions=len(open_positions),
            open=[{"symbol": p.symbol, "amt": p.position_amt, "entry": p.entry_price} for p in open_positions],
        )

        is_hedge_mode = await client.get_position_mode()
        if is_hedge_mode:
            if open_positions:
                log_event(
                    _LOG, "cannot_switch_position_mode", level=40,
                    message="Account is in Hedge mode with open positions - BingX refuses to switch while "
                            "any position/order is open. Close everything manually on BingX first, then rerun.",
                )
                return
            await client.set_position_mode(hedge_mode=False)
            log_event(_LOG, "position_mode_switched_to_one_way")
        else:
            log_event(_LOG, "position_mode_already_one_way")

        rules = await client.get_symbol_rules()
        log_event(_LOG, "symbol_rules_ok", symbol_count=len(rules), has_btcusdt="BTCUSDT" in rules)

        log_event(
            _LOG, "verified", message=(
                "Credentials are valid, VST balance is usable, and the account is in one-way position "
                "mode. Safe to proceed to scripts/verify_bingx_shadow_trading.py."
            ),
        )
    except (BingXOrderError, BingXRestError) as exc:
        log_event(
            _LOG, "verification_failed", level=40, error=str(exc),
            message="Fix this before attempting any order placement - see error above",
        )
    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(_main())
