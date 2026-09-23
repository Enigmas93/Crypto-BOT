#!/usr/bin/env python
"""Demonstrates and validates the Phase 7b global Kill Switch against real
data - spec section 23.

Same shape as demo_risk_engine.py (not a poller - a synchronous gate a
caller checks before/around RiskEngine.evaluate()). This script:

  1. Pulls a real BTCUSDT price from Binance, same as demo_risk_engine.py.
  2. Drives a real database-backed demo account into a drawdown breach via
     RiskRepository, and shows KillSwitchRepository.check_and_maybe_trigger
     tripping and persisting it.
  3. Shows the trip is STICKY: even after the account's drawdown recovers
     back under the threshold (a winning trade), the kill switch stays
     tripped - re-checking must NOT clear it and must NOT write a second
     TRIGGERED event.
  4. Shows the composition pattern a real caller uses: check the kill
     switch FIRST, and only call RiskEngine.evaluate() if it's clear. With
     the switch tripped, a perfectly healthy trade proposal is blocked by
     KILL_SWITCH_TRIGGERED alone, before RiskEngine ever runs.
  5. Shows the only way out: an explicit reset() with an operator note,
     logged to kill_switch_events, after which a fresh breach can trip it
     again.

Usage (from backend/, venv active):
    python scripts/demo_kill_switch.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aegis.config import get_settings  # noqa: E402
from aegis.db.engine import close_pool, create_pool  # noqa: E402
from aegis.db.kill_switch_repository import KillSwitchRepository  # noqa: E402
from aegis.db.risk_repository import RiskRepository  # noqa: E402
from aegis.logging_utils import configure_logging, get_logger, log_event  # noqa: E402
from aegis.providers.binance.rest_client import BinanceFuturesRestClient  # noqa: E402
from aegis.risk.service import RiskEngine, TradeProposal

_LOG = get_logger("scripts.demo_kill_switch")
DEMO_ACCOUNT = "demo_kill_switch"


async def _main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)

    rest = BinanceFuturesRestClient(testnet=settings.binance_testnet)
    pool = await create_pool(settings)
    risk_repo = RiskRepository(pool)
    kill_switch_repo = KillSwitchRepository(pool)
    risk_engine = RiskEngine(settings)

    try:
        rules = await rest.get_symbol_rules()
        btc_rules = rules["BTCUSDT"]
        klines = await rest.get_klines("BTCUSDT", "1m", limit=1)
        current_price = klines[-1].close
        log_event(_LOG, "real_market_data", symbol="BTCUSDT", price=current_price)

        # Start every demo run from a clean, untriggered $1,000 account.
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM risk_account_state WHERE account_id = $1", DEMO_ACCOUNT)
            await conn.execute("DELETE FROM kill_switch_state WHERE account_id = $1", DEMO_ACCOUNT)
            await conn.execute("DELETE FROM kill_switch_events WHERE account_id = $1", DEMO_ACCOUNT)
        account = await risk_repo.initialize_account_state(DEMO_ACCOUNT, starting_equity=1000.0)

        healthy_proposal = TradeProposal(
            symbol="BTCUSDT", side="LONG", entry_price=current_price,
            stop_price=current_price * 0.98, take_profit_price=current_price * 1.04, leverage=3,
        )

        log_event(_LOG, "step_1_baseline", message="Before any breach, the switch must be clear")
        state = await kill_switch_repo.get_state(DEMO_ACCOUNT)
        log_event(_LOG, "kill_switch_state", is_triggered=state.is_triggered)
        assert state.is_triggered is False

        log_event(
            _LOG, "step_2_drive_a_drawdown_breach",
            message=f"One large loss past max_drawdown={settings.max_drawdown:.0%} of peak equity",
        )
        loss_amount = 1000.0 * (settings.max_drawdown + 0.02)  # comfortably past the threshold
        account = await risk_repo.record_trade_outcome(DEMO_ACCOUNT, -loss_amount)
        state = await kill_switch_repo.check_and_maybe_trigger(
            DEMO_ACCOUNT, account.equity, account.peak_equity, account.consecutive_losses, settings,
        )
        log_event(
            _LOG, "kill_switch_state", is_triggered=state.is_triggered, reasons=state.reasons,
            equity=round(account.equity, 2), peak_equity=round(account.peak_equity, 2),
        )
        assert state.is_triggered is True
        assert state.reasons == ["MAX_DRAWDOWN_BREACHED"]

        log_event(
            _LOG, "step_3_recovery_does_not_clear_it",
            message="A big win brings equity back near peak - a naive re-evaluation would say 'no breach', "
                    "but the switch must stay tripped",
        )
        account = await risk_repo.record_trade_outcome(DEMO_ACCOUNT, loss_amount * 0.95)
        recovered_drawdown_pct = (account.peak_equity - account.equity) / account.peak_equity
        state = await kill_switch_repo.check_and_maybe_trigger(
            DEMO_ACCOUNT, account.equity, account.peak_equity, account.consecutive_losses, settings,
        )
        log_event(
            _LOG, "kill_switch_state", is_triggered=state.is_triggered,
            recovered_drawdown_pct=round(recovered_drawdown_pct, 4),
            below_threshold=recovered_drawdown_pct < settings.max_drawdown,
        )
        assert state.is_triggered is True, "kill switch must stay sticky through a recovery"
        events = await kill_switch_repo.fetch_events(DEMO_ACCOUNT)
        triggered_events = [e for e in events if e["action"] == "TRIGGERED"]
        assert len(triggered_events) == 1, "re-checking an already-triggered switch must not log a second event"

        log_event(
            _LOG, "step_4_a_healthy_proposal_is_blocked_by_the_switch_alone",
            message="Caller composition: check the kill switch BEFORE RiskEngine.evaluate()",
        )
        state = await kill_switch_repo.get_state(DEMO_ACCOUNT)
        if state.is_triggered:
            log_event(_LOG, "trade_blocked", decision="BLOCK", reasons=["KILL_SWITCH_TRIGGERED"] + (state.reasons or []))
        else:
            decision = risk_engine.evaluate(healthy_proposal, account, btc_rules)
            log_event(_LOG, "trade_decision", decision=decision.decision, reasons=decision.reasons)
        assert state.is_triggered is True  # RiskEngine never even ran

        log_event(_LOG, "step_5_manual_reset", message="The only way back to trading")
        state = await kill_switch_repo.reset(DEMO_ACCOUNT, note="demo script - reviewed manually, resetting")
        log_event(_LOG, "kill_switch_state", is_triggered=state.is_triggered)
        assert state.is_triggered is False

        decision = risk_engine.evaluate(healthy_proposal, account, btc_rules)
        log_event(_LOG, "trade_decision_after_reset", decision=decision.decision, reasons=decision.reasons)

        log_event(_LOG, "step_6_a_fresh_breach_can_trip_it_again", message="Loss-streak trigger this time")
        for _ in range(settings.loss_streak_halt_threshold):
            account = await risk_repo.record_trade_outcome(DEMO_ACCOUNT, -1.0)
        state = await kill_switch_repo.check_and_maybe_trigger(
            DEMO_ACCOUNT, account.equity, account.peak_equity, account.consecutive_losses, settings,
        )
        log_event(_LOG, "kill_switch_state", is_triggered=state.is_triggered, reasons=state.reasons)
        assert state.is_triggered is True
        assert state.reasons == ["LOSS_STREAK_HALT_THRESHOLD"]

        events = await kill_switch_repo.fetch_events(DEMO_ACCOUNT)
        log_event(_LOG, "full_audit_trail", actions=[e["action"] for e in reversed(events)])
    finally:
        await rest.aclose()
        await close_pool(pool)


if __name__ == "__main__":
    asyncio.run(_main())
