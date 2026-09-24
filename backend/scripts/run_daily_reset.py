#!/usr/bin/env python
"""Resets every trading account's daily PnL baseline once per UTC day -
a real gap found during a full system audit (2026-09-25).

`RiskRepository.reset_daily()` (spec section 20 - the mechanism
`max_daily_loss` depends on) has existed since Phase 7, but nothing ever
called it outside of `demo_risk_engine.py`'s manual demonstration and its
own unit test. Every account's `daily_realized_pnl` had been accumulating
since the account's initial creation, NEVER resetting - `DAILY_LOSS_LIMIT`
was silently comparing against lifetime cumulative PnL, not the current
day's, and the dashboard's "PnL do dia" was actually "PnL desde a
criação da conta" for every account that had been running more than a day.

Restart-safe without a dedicated tracking column: on each check, an
account is reset only if `risk_account_state.updated_at` (bumped by
`reset_daily` itself, along with every other write to that row) is still
dated a PREVIOUS UTC day - so restarting this script mid-day never
re-zeroes an account that was already reset (or otherwise touched) today,
and an account with zero activity all day still gets reset right on
schedule the moment this script next checks.

Usage (from backend/, venv active):
    python scripts/run_daily_reset.py

Ctrl+C to stop.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aegis.config import get_settings  # noqa: E402
from aegis.db.engine import close_pool, create_pool  # noqa: E402
from aegis.db.risk_repository import RiskRepository  # noqa: E402
from aegis.logging_utils import configure_logging, get_logger, log_event  # noqa: E402

_LOG = get_logger("scripts.run_daily_reset")
# Fase 17: shadow_bingx/momentum_bingx split into _demo/_live variants (see
# migration 0018) - both are listed regardless of which one is currently
# active, so a dormant mode's daily baseline is still correct the moment
# it's switched back to.
_ACCOUNTS = (
    "paper", "shadow", "shadow_bingx_demo", "shadow_bingx_live",
    "momentum", "momentum_bingx_demo", "momentum_bingx_live",
)
_CHECK_INTERVAL_SECONDS = 60.0


async def check_and_reset_stale_accounts(risk_repo, accounts, today) -> list[str]:
    """One pass: resets any account whose state hasn't been touched since
    a previous UTC day. Returns the account_ids actually reset (empty list
    on a normal cycle where everyone's already current) - pulled out of
    the poll loop so it's directly unit-testable without a live clock or
    a real sleep loop, same reasoning as
    `scripts.supervisor._Supervised.check_and_maybe_restart`."""
    reset_accounts = []
    for account_id in accounts:
        updated_at = await risk_repo.get_updated_at(account_id)
        if updated_at is None:
            continue  # not initialized yet - the trading engine will do that itself
        if updated_at.date() < today:
            state = await risk_repo.reset_daily(account_id)
            log_event(
                _LOG, "daily_reset", account_id=account_id,
                new_daily_starting_equity=round(state.daily_starting_equity, 2),
                previous_updated_at=updated_at.isoformat(),
            )
            reset_accounts.append(account_id)
    return reset_accounts


async def _main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)

    pool = await create_pool(settings)
    risk_repo = RiskRepository(pool)
    try:
        log_event(_LOG, "startup", accounts=_ACCOUNTS, check_interval=_CHECK_INTERVAL_SECONDS)
        while True:
            await check_and_reset_stale_accounts(risk_repo, _ACCOUNTS, datetime.now(UTC).date())
            await asyncio.sleep(_CHECK_INTERVAL_SECONDS)
    finally:
        await close_pool(pool)


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
