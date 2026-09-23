"""Integration tests for RiskRepository against a real TimescaleDB.
Skipped automatically if the database is unreachable (see conftest.py).
"""
from __future__ import annotations

import asyncio
import uuid

import pytest

from aegis.db.risk_repository import RiskRepository
from aegis.providers.binance.models import SymbolRules
from aegis.risk.service import RiskDecision
from aegis.risk.sizing import PositionSizeResult


def _account_id() -> str:
    return f"test_{uuid.uuid4().hex[:8]}"


@pytest.mark.asyncio
async def test_initialize_account_state_is_idempotent(pool):
    repo = RiskRepository(pool)
    account_id = _account_id()

    first = await repo.initialize_account_state(account_id, starting_equity=1000.0)
    assert first.equity == pytest.approx(1000.0)
    assert first.peak_equity == pytest.approx(1000.0)

    # calling init again must NOT reset a state that already exists
    await repo.record_trade_outcome(account_id, 50.0)
    second_init = await repo.initialize_account_state(account_id, starting_equity=1000.0)
    assert second_init.equity == pytest.approx(1050.0)


@pytest.mark.asyncio
async def test_get_account_state_returns_none_for_unknown_account(pool):
    repo = RiskRepository(pool)
    assert await repo.get_account_state(_account_id()) is None


@pytest.mark.asyncio
async def test_record_trade_outcome_updates_equity_and_peak(pool):
    repo = RiskRepository(pool)
    account_id = _account_id()
    await repo.initialize_account_state(account_id, starting_equity=1000.0)

    after_win = await repo.record_trade_outcome(account_id, 100.0)
    assert after_win.equity == pytest.approx(1100.0)
    assert after_win.peak_equity == pytest.approx(1100.0)
    assert after_win.consecutive_losses == 0

    after_loss = await repo.record_trade_outcome(account_id, -50.0)
    assert after_loss.equity == pytest.approx(1050.0)
    assert after_loss.peak_equity == pytest.approx(1100.0)  # peak doesn't fall with a loss
    assert after_loss.consecutive_losses == 1


@pytest.mark.asyncio
async def test_record_trade_outcome_resets_streak_on_a_win(pool):
    repo = RiskRepository(pool)
    account_id = _account_id()
    await repo.initialize_account_state(account_id, starting_equity=1000.0)

    await repo.record_trade_outcome(account_id, -10.0)
    await repo.record_trade_outcome(account_id, -10.0)
    after_two_losses = await repo.record_trade_outcome(account_id, -10.0)
    assert after_two_losses.consecutive_losses == 3

    after_win = await repo.record_trade_outcome(account_id, 5.0)
    assert after_win.consecutive_losses == 0


@pytest.mark.asyncio
async def test_record_trade_outcome_raises_for_unknown_account(pool):
    repo = RiskRepository(pool)
    with pytest.raises(ValueError):
        await repo.record_trade_outcome(_account_id(), 10.0)


@pytest.mark.asyncio
async def test_reset_daily_carries_equity_forward_and_zeroes_pnl(pool):
    repo = RiskRepository(pool)
    account_id = _account_id()
    await repo.initialize_account_state(account_id, starting_equity=1000.0)
    await repo.record_trade_outcome(account_id, -30.0)

    state = await repo.reset_daily(account_id)

    assert state.daily_starting_equity == pytest.approx(970.0)
    assert state.daily_realized_pnl == pytest.approx(0.0)
    assert state.equity == pytest.approx(970.0)  # equity itself is untouched by the reset


@pytest.mark.asyncio
async def test_set_exposure_updates_positions_and_correlation(pool):
    repo = RiskRepository(pool)
    account_id = _account_id()
    await repo.initialize_account_state(account_id, starting_equity=1000.0)

    await repo.set_exposure(account_id, open_positions_count=3, correlated_exposure_pct=0.22)

    state = await repo.get_account_state(account_id)
    assert state.open_positions_count == 3
    assert state.correlated_exposure_pct == pytest.approx(0.22)


@pytest.mark.asyncio
async def test_insert_and_fetch_risk_events(pool):
    repo = RiskRepository(pool)
    account_id = _account_id()
    decision = RiskDecision(
        decision="BLOCK", reasons=["NO_STOP"], drawdown_state="NORMAL", drawdown_pct=0.0,
        loss_streak_action="NONE", effective_risk_per_trade=0.005,
        position_size=PositionSizeResult("NO_STOP_DISTANCE", 0.0, 0.0, 5.0, 0.0),
        net_r_multiple=None,
        liquidation_check=None,  # type: ignore[arg-type]  # not needed for this row
    )
    # liquidation_check is normally always populated by RiskEngine.evaluate();
    # the repository doesn't touch it, so None here is fine for a persistence test

    await repo.insert_risk_event(account_id, "BTCUSDT", "LONG", decision, strategy_id="test_strategy")

    events = await repo.fetch_recent_events(account_id, limit=10)
    assert len(events) == 1
    assert events[0]["symbol"] == "BTCUSDT"
    assert events[0]["decision"] == "BLOCK"
    assert events[0]["reasons"] == ["NO_STOP"]


@pytest.mark.asyncio
async def test_fetch_recent_events_orders_newest_first(pool):
    repo = RiskRepository(pool)
    account_id = _account_id()
    rules = SymbolRules(symbol="BTCUSDT", status="TRADING", price_precision=2, quantity_precision=3,
                         tick_size=0.1, step_size=0.001, min_notional=5.0)

    def _pass_decision() -> RiskDecision:
        return RiskDecision(
            decision="PASS", reasons=[], drawdown_state="NORMAL", drawdown_pct=0.0,
            loss_streak_action="NONE", effective_risk_per_trade=0.005,
            position_size=PositionSizeResult("OK", 0.01, 500.0, 5.0, 500.0),
            net_r_multiple=2.0, liquidation_check=None,  # type: ignore[arg-type]
        )

    await repo.insert_risk_event(account_id, "BTCUSDT", "LONG", _pass_decision())
    await asyncio.sleep(0.01)  # guarantee a distinct occurred_at (server-generated) from the first insert
    await repo.insert_risk_event(account_id, "ETHUSDT", "SHORT", _pass_decision())

    events = await repo.fetch_recent_events(account_id, limit=10)
    assert len(events) == 2
    assert events[0]["symbol"] == "ETHUSDT"  # most recently inserted
