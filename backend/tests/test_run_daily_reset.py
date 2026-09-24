"""Offline tests for scripts/run_daily_reset.py's core decision logic -
found missing during a full system audit (2026-09-25): RiskRepository.
reset_daily() existed since Phase 7 but nothing ever called it outside a
demo script, so every account's "daily" PnL had actually been accumulating
since account creation, never resetting.
"""
from __future__ import annotations

import sys
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import run_daily_reset  # noqa: E402


class _FakeRiskRepo:
    def __init__(self, updated_at_by_account: dict, reset_now: datetime | None = None):
        self._updated_at = dict(updated_at_by_account)
        self.reset_calls: list[str] = []
        # A fixed, test-controlled "now" for reset_daily to stamp - the
        # real risk_account_state.updated_at is server-clock `now()`, but
        # a test needs it pinned to whatever fictional "today" it's using,
        # not this machine's real wall-clock date.
        self._reset_now = reset_now or datetime.now(UTC)

    async def get_updated_at(self, account_id):
        return self._updated_at.get(account_id)

    async def reset_daily(self, account_id):
        self.reset_calls.append(account_id)
        self._updated_at[account_id] = self._reset_now

        class _State:
            daily_starting_equity = 1000.0
        return _State()


@pytest.mark.asyncio
async def test_resets_only_accounts_stale_since_a_previous_day():
    today = date(2026, 9, 25)
    yesterday = datetime(2026, 9, 24, 10, 0, tzinfo=UTC)
    today_already = datetime(2026, 9, 25, 3, 0, tzinfo=UTC)
    repo = _FakeRiskRepo({"shadow": yesterday, "paper": today_already})

    reset = await run_daily_reset.check_and_reset_stale_accounts(repo, ["shadow", "paper"], today)

    assert reset == ["shadow"]
    assert repo.reset_calls == ["shadow"]


@pytest.mark.asyncio
async def test_skips_accounts_not_yet_initialized():
    repo = _FakeRiskRepo({})  # get_updated_at returns None for everything

    reset = await run_daily_reset.check_and_reset_stale_accounts(repo, ["paper", "shadow"], date(2026, 9, 25))

    assert reset == []
    assert repo.reset_calls == []


@pytest.mark.asyncio
async def test_a_second_pass_the_same_day_does_not_reset_again():
    """Restart-safety: after a reset, updated_at moves to "now" - a second
    check later the same day must not fire again."""
    today = date(2026, 9, 25)
    repo = _FakeRiskRepo(
        {"shadow": datetime(2026, 9, 24, 10, 0, tzinfo=UTC)},
        reset_now=datetime(2026, 9, 25, 3, 0, tzinfo=UTC),
    )

    first = await run_daily_reset.check_and_reset_stale_accounts(repo, ["shadow"], today)
    second = await run_daily_reset.check_and_reset_stale_accounts(repo, ["shadow"], today)

    assert first == ["shadow"]
    assert second == []
    assert repo.reset_calls == ["shadow"]


def test_every_tracked_account_is_a_real_dashboard_account():
    # Regression: a typo here would silently stop resetting an account's
    # daily PnL forever, exactly like the original bug.
    assert set(run_daily_reset._ACCOUNTS) == {
        "paper", "shadow", "shadow_bingx_demo", "shadow_bingx_live",
        "momentum", "momentum_bingx_demo", "momentum_bingx_live",
    }
