"""Integration tests for KillSwitchRepository against a real TimescaleDB.
Skipped automatically if the database is unreachable (see conftest.py).
"""
from __future__ import annotations

import uuid

import pytest

from aegis.db.kill_switch_repository import KillSwitchRepository


def _account_id() -> str:
    return f"testks_{uuid.uuid4().hex[:8]}"


class _Settings:
    max_drawdown = 0.10
    loss_streak_halt_threshold = 8


@pytest.mark.asyncio
async def test_get_state_for_unknown_account_is_not_triggered(pool):
    repo = KillSwitchRepository(pool)
    state = await repo.get_state(_account_id())
    assert state.is_triggered is False
    assert state.reasons is None


@pytest.mark.asyncio
async def test_check_and_maybe_trigger_does_nothing_under_normal_conditions(pool):
    repo = KillSwitchRepository(pool)
    account_id = _account_id()

    state = await repo.check_and_maybe_trigger(
        account_id, equity=1000.0, peak_equity=1000.0, consecutive_losses=0, settings=_Settings(),
    )
    assert state.is_triggered is False


@pytest.mark.asyncio
async def test_check_and_maybe_trigger_trips_on_max_drawdown(pool):
    repo = KillSwitchRepository(pool)
    account_id = _account_id()

    state = await repo.check_and_maybe_trigger(
        account_id, equity=880.0, peak_equity=1000.0, consecutive_losses=0, settings=_Settings(),
    )
    assert state.is_triggered is True
    assert state.reasons == ["MAX_DRAWDOWN_BREACHED"]
    assert state.triggered_at is not None

    events = await repo.fetch_events(account_id)
    assert len(events) == 1
    assert events[0]["action"] == "TRIGGERED"
    assert events[0]["reasons"] == ["MAX_DRAWDOWN_BREACHED"]


@pytest.mark.asyncio
async def test_check_and_maybe_trigger_is_sticky_and_does_not_re_log_once_triggered(pool):
    """Core correctness property of a kill switch: once tripped, it stays
    tripped even if the underlying condition would no longer fire, and
    checking again must not spam kill_switch_events with duplicate rows."""
    repo = KillSwitchRepository(pool)
    account_id = _account_id()

    await repo.check_and_maybe_trigger(
        account_id, equity=880.0, peak_equity=1000.0, consecutive_losses=0, settings=_Settings(),
    )
    # equity has since "recovered" past the drawdown threshold - a naive
    # re-evaluation would say "no trigger", but the switch must stay tripped
    state = await repo.check_and_maybe_trigger(
        account_id, equity=999.0, peak_equity=1000.0, consecutive_losses=0, settings=_Settings(),
    )
    assert state.is_triggered is True

    events = await repo.fetch_events(account_id)
    assert len(events) == 1  # still just the one TRIGGERED row, not two


@pytest.mark.asyncio
async def test_reset_clears_state_and_logs_an_event_with_the_note(pool):
    repo = KillSwitchRepository(pool)
    account_id = _account_id()
    await repo.check_and_maybe_trigger(
        account_id, equity=880.0, peak_equity=1000.0, consecutive_losses=0, settings=_Settings(),
    )

    state = await repo.reset(account_id, note="reviewed manually, strategy paused for recalibration")

    assert state.is_triggered is False
    assert state.reasons is None
    assert state.triggered_at is None

    events = await repo.fetch_events(account_id)
    assert events[0]["action"] == "RESET"
    assert events[0]["note"] == "reviewed manually, strategy paused for recalibration"


@pytest.mark.asyncio
async def test_reset_raises_when_not_currently_triggered(pool):
    repo = KillSwitchRepository(pool)
    account_id = _account_id()
    with pytest.raises(ValueError):
        await repo.reset(account_id, note="should fail")


@pytest.mark.asyncio
async def test_after_reset_a_new_breach_can_trigger_again(pool):
    repo = KillSwitchRepository(pool)
    account_id = _account_id()
    await repo.check_and_maybe_trigger(
        account_id, equity=880.0, peak_equity=1000.0, consecutive_losses=0, settings=_Settings(),
    )
    await repo.reset(account_id, note="cleared for a fresh start")

    state = await repo.check_and_maybe_trigger(
        account_id, equity=1000.0, peak_equity=1000.0, consecutive_losses=8, settings=_Settings(),
    )
    assert state.is_triggered is True
    assert state.reasons == ["LOSS_STREAK_HALT_THRESHOLD"]

    events = await repo.fetch_events(account_id)
    assert [e["action"] for e in events] == ["TRIGGERED", "RESET", "TRIGGERED"]
