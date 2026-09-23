from datetime import UTC, datetime

import pytest

from aegis.backtest.models import BacktestTrade
from aegis.backtest.monte_carlo import run_monte_carlo

_T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _trade(net_pnl: float) -> BacktestTrade:
    return BacktestTrade(
        symbol="BTCUSDT", side="LONG", entry_time=_T0, entry_price=100.0,
        exit_time=_T0, exit_price=100.0, exit_reason="TAKE_PROFIT", quantity=1.0,
        gross_pnl=net_pnl, fees_paid=0.0, net_pnl=net_pnl, r_multiple=net_pnl / 50.0,
        confluence_score=50.0, reasons=[],
    )


def test_zero_trades_raises():
    with pytest.raises(ValueError):
        run_monte_carlo([], initial_equity=1000.0)


def test_zero_simulations_raises():
    with pytest.raises(ValueError):
        run_monte_carlo([_trade(10.0)], initial_equity=1000.0, num_simulations=0)


def test_same_seed_is_fully_reproducible():
    trades = [_trade(50.0), _trade(-30.0), _trade(80.0), _trade(-20.0), _trade(10.0)]
    a = run_monte_carlo(trades, initial_equity=1000.0, num_simulations=500, seed=42)
    b = run_monte_carlo(trades, initial_equity=1000.0, num_simulations=500, seed=42)
    assert a == b


def test_different_seeds_generally_differ():
    trades = [_trade(50.0), _trade(-30.0), _trade(80.0), _trade(-20.0), _trade(10.0)]
    a = run_monte_carlo(trades, initial_equity=1000.0, num_simulations=500, seed=1)
    b = run_monte_carlo(trades, initial_equity=1000.0, num_simulations=500, seed=2)
    assert a.final_equity_p50 != b.final_equity_p50


def test_percentiles_are_monotonically_non_decreasing():
    trades = [_trade(50.0), _trade(-30.0), _trade(80.0), _trade(-20.0), _trade(10.0), _trade(-60.0)]
    result = run_monte_carlo(trades, initial_equity=1000.0, num_simulations=1000, seed=7)
    assert result.final_equity_p5 <= result.final_equity_p25 <= result.final_equity_p50
    assert result.final_equity_p50 <= result.final_equity_p75 <= result.final_equity_p95
    assert result.max_drawdown_pct_p50 <= result.max_drawdown_pct_p95 <= result.max_drawdown_pct_worst


def test_all_winning_trades_never_ruins_and_has_zero_drawdown():
    trades = [_trade(10.0), _trade(20.0), _trade(15.0)]
    result = run_monte_carlo(trades, initial_equity=1000.0, num_simulations=500, seed=3)
    assert result.probability_of_ruin == 0.0
    assert result.max_drawdown_pct_worst == pytest.approx(0.0)
    assert result.final_equity_p5 > 1000.0


def test_a_single_catastrophic_loss_produces_certain_ruin():
    # Every resample is built from this one trade, repeated - if the only
    # trade that exists wipes the whole account, every simulated path must
    # too, deterministically, regardless of the seed.
    trades = [_trade(-1000.0)]
    result = run_monte_carlo(trades, initial_equity=1000.0, num_simulations=200, seed=9)
    assert result.probability_of_ruin == 1.0
    assert result.final_equity_p50 == pytest.approx(0.0)


def test_result_reports_the_inputs_it_was_given():
    trades = [_trade(10.0), _trade(-5.0)]
    result = run_monte_carlo(trades, initial_equity=2500.0, num_simulations=300, seed=1)
    assert result.num_simulations == 300
    assert result.num_trades == 2
    assert result.initial_equity == pytest.approx(2500.0)
