"""Integration tests for WalkForwardRepository against a real TimescaleDB.
Skipped automatically if the database is unreachable (see conftest.py).
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from aegis.backtest.walk_forward import WalkForwardFold, WalkForwardResult
from aegis.db.walk_forward_repository import WalkForwardRepository
from aegis.strategy.strategies import ALL_STRATEGY_IDS


class _FakeMetrics:
    def __init__(self, total_trades, win_rate, net_pnl, max_drawdown_pct):
        self.total_trades = total_trades
        self.win_rate = win_rate
        self.net_pnl = net_pnl
        self.max_drawdown_pct = max_drawdown_pct


class _FakeResult:
    def __init__(self, net_pnl, kill_switch_triggered=False):
        self.metrics = _FakeMetrics(total_trades=5, win_rate=0.6, net_pnl=net_pnl, max_drawdown_pct=0.05)
        self.kill_switch_triggered = kill_switch_triggered


def _walk_forward_result(symbol="BTCUSDT") -> WalkForwardResult:
    folds = [
        WalkForwardFold(fold_index=0, window_start=datetime(2026, 1, 1, tzinfo=UTC),
                         window_end=datetime(2026, 1, 15, tzinfo=UTC), result=_FakeResult(net_pnl=50.0)),
        WalkForwardFold(fold_index=1, window_start=datetime(2026, 1, 15, tzinfo=UTC),
                         window_end=datetime(2026, 1, 30, tzinfo=UTC), result=_FakeResult(net_pnl=-20.0)),
    ]
    return WalkForwardResult(
        symbol=symbol, interval="1h", window_bars=500, step_bars=250, folds=folds,
        profitable_fold_pct=0.5, fold_net_pnls=[50.0, -20.0], fold_win_rates=[0.6, 0.6],
        average_net_pnl=15.0, worst_fold_net_pnl=-20.0, any_fold_kill_switch_triggered=False,
    )


@pytest.mark.asyncio
async def test_save_and_fetch_recent_runs(pool):
    repo = WalkForwardRepository(pool)
    result = _walk_forward_result(symbol="TESTWFCOIN")

    run_id = await repo.save_run(result, ALL_STRATEGY_IDS)
    recent = await repo.fetch_recent_runs(limit=5)

    assert any(r["id"] == run_id for r in recent)
    saved = next(r for r in recent if r["id"] == run_id)
    assert saved["symbol"] == "TESTWFCOIN"
    assert saved["fold_count"] == 2
    assert saved["profitable_fold_pct"] == pytest.approx(0.5)
    assert saved["worst_fold_net_pnl"] == pytest.approx(-20.0)


@pytest.mark.asyncio
async def test_fetch_run_includes_fold_detail(pool):
    repo = WalkForwardRepository(pool)
    result = _walk_forward_result(symbol="TESTWFCOIN2")
    run_id = await repo.save_run(result, ALL_STRATEGY_IDS)

    run = await repo.fetch_run(run_id)

    assert run is not None
    assert run["symbol"] == "TESTWFCOIN2"
    assert len(run["folds"]) == 2
    assert run["folds"][0]["net_pnl"] == pytest.approx(50.0)
    assert run["folds"][1]["net_pnl"] == pytest.approx(-20.0)


@pytest.mark.asyncio
async def test_fetch_run_returns_none_for_unknown_id(pool):
    repo = WalkForwardRepository(pool)
    assert await repo.fetch_run(999_999_999) is None
