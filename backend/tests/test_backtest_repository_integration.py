"""Integration tests for BacktestRepository against a real TimescaleDB.
Skipped automatically if the database is unreachable (see conftest.py).
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

import pytest

from aegis.backtest.metrics import BacktestMetrics
from aegis.backtest.models import BacktestConfig, BacktestResult, BacktestTrade
from aegis.db.backtest_repository import BacktestRepository

_T0 = datetime(2026, 1, 1, tzinfo=UTC)
_T1 = datetime(2026, 1, 2, tzinfo=UTC)


def _symbol() -> str:
    return f"TESTBT{uuid.uuid4().hex[:8].upper()}"


def _metrics(total_trades: int = 1) -> BacktestMetrics:
    return BacktestMetrics(
        total_trades=total_trades, wins=1, losses=0, win_rate=1.0, loss_rate=0.0,
        gross_profit=10.0, gross_loss=0.0, net_pnl=10.0, profit_factor=None,
        average_win=10.0, average_loss=0.0, expectancy=10.0, total_fees=0.5,
        max_drawdown_pct=0.02, recovery_factor=None, average_r=2.0, median_r=2.0,
        sharpe_r=None, sortino_r=None, calmar_r=None, max_consecutive_losses=0,
        total_return_pct=1.0, final_equity=1010.0,
    )


def _trade(symbol: str) -> BacktestTrade:
    return BacktestTrade(
        symbol=symbol, side="LONG", entry_time=_T0, entry_price=100.0,
        exit_time=_T1, exit_price=105.0, exit_reason="TAKE_PROFIT", quantity=1.0,
        gross_pnl=5.0, fees_paid=0.5, net_pnl=4.5, r_multiple=2.0,
        confluence_score=45.0, reasons=["breakout_confirmed"],
    )


def _result(
    symbol: str, trades: list[BacktestTrade], strategy_weights=None,
    kill_switch_triggered: bool = False, kill_switch_reasons=None, kill_switch_tripped_at=None,
) -> BacktestResult:
    config = BacktestConfig(symbol=symbol, interval="1h", strategy_weights=strategy_weights)
    return BacktestResult(
        config=config, trades=trades, equity_curve=[(_T1, 1010.0)],
        metrics=_metrics(total_trades=len(trades)),
        data_start=_T0, data_end=_T1, total_bars=250,
        kill_switch_triggered=kill_switch_triggered,
        kill_switch_reasons=kill_switch_reasons or [],
        kill_switch_tripped_at=kill_switch_tripped_at,
    )


@pytest.mark.asyncio
async def test_save_run_persists_run_and_trades(pool):
    repo = BacktestRepository(pool)
    symbol = _symbol()
    result = _result(symbol, [_trade(symbol)])

    run_id = await repo.save_run(result)

    run = await repo.fetch_run(run_id)
    assert run["symbol"] == symbol
    assert run["interval"] == "1h"
    assert run["total_trades"] == 1
    assert run["wins"] == 1
    assert run["final_equity"] == pytest.approx(1010.0)
    assert run["total_bars"] == 250
    assert run["data_start"] == _T0
    assert run["data_end"] == _T1
    assert run["kill_switch_triggered"] is False
    assert run["kill_switch_reasons"] is None
    assert run["kill_switch_tripped_at"] is None

    trades = await repo.fetch_trades(run_id)
    assert len(trades) == 1
    assert trades[0]["symbol"] == symbol
    assert trades[0]["side"] == "LONG"
    assert trades[0]["net_pnl"] == pytest.approx(4.5)
    assert trades[0]["reasons"] == ["breakout_confirmed"]


@pytest.mark.asyncio
async def test_save_run_persists_a_kill_switch_trip(pool):
    repo = BacktestRepository(pool)
    symbol = _symbol()
    result = _result(
        symbol, [_trade(symbol)],
        kill_switch_triggered=True, kill_switch_reasons=["LOSS_STREAK_HALT_THRESHOLD"],
        kill_switch_tripped_at=_T1,
    )

    run_id = await repo.save_run(result)

    run = await repo.fetch_run(run_id)
    assert run["kill_switch_triggered"] is True
    assert run["kill_switch_reasons"] == ["LOSS_STREAK_HALT_THRESHOLD"]
    assert run["kill_switch_tripped_at"] == _T1


@pytest.mark.asyncio
async def test_save_run_with_no_trades_persists_an_empty_run(pool):
    repo = BacktestRepository(pool)
    symbol = _symbol()
    result = _result(symbol, [])

    run_id = await repo.save_run(result)

    run = await repo.fetch_run(run_id)
    assert run["total_trades"] == 0
    trades = await repo.fetch_trades(run_id)
    assert trades == []


@pytest.mark.asyncio
async def test_fetch_run_returns_none_for_unknown_id(pool):
    repo = BacktestRepository(pool)
    assert await repo.fetch_run(-1) is None


@pytest.mark.asyncio
async def test_strategy_weights_round_trip_as_a_dict_not_a_raw_string(pool):
    repo = BacktestRepository(pool)
    symbol = _symbol()
    weights = {"TREND_PULLBACK": 1.5, "BREAKOUT": 0.5}
    result = _result(symbol, [], strategy_weights=weights)

    run_id = await repo.save_run(result)
    run = await repo.fetch_run(run_id)

    assert run["strategy_weights"] == weights


@pytest.mark.asyncio
async def test_strategy_weights_none_when_not_provided(pool):
    repo = BacktestRepository(pool)
    symbol = _symbol()
    result = _result(symbol, [])

    run_id = await repo.save_run(result)
    run = await repo.fetch_run(run_id)

    assert run["strategy_weights"] is None


@pytest.mark.asyncio
async def test_fetch_recent_runs_orders_newest_first_and_filters_by_symbol(pool):
    repo = BacktestRepository(pool)
    symbol_a = _symbol()
    symbol_b = _symbol()

    id_1 = await repo.save_run(_result(symbol_a, []))
    await asyncio.sleep(0.01)  # guarantee a distinct created_at (server-generated) from the first insert
    id_2 = await repo.save_run(_result(symbol_a, []))
    await repo.save_run(_result(symbol_b, []))

    runs = await repo.fetch_recent_runs(symbol=symbol_a, limit=10)
    assert [r["id"] for r in runs] == [id_2, id_1]  # newest first
    assert all(r["symbol"] == symbol_a for r in runs)
