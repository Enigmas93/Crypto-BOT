from datetime import UTC, datetime

import pytest

from aegis.backtest.metrics import compute_metrics
from aegis.backtest.models import BacktestTrade

_T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _trade(net_pnl: float, r_multiple: float, fees: float = 1.0) -> BacktestTrade:
    return BacktestTrade(
        symbol="BTCUSDT", side="LONG", entry_time=_T0, entry_price=100.0,
        exit_time=_T0, exit_price=100.0, exit_reason="TAKE_PROFIT", quantity=1.0,
        gross_pnl=net_pnl + fees, fees_paid=fees, net_pnl=net_pnl, r_multiple=r_multiple,
        confluence_score=50.0, reasons=[],
    )


def test_no_trades_gives_safe_defaults():
    metrics = compute_metrics([], [], initial_equity=1000.0)
    assert metrics.total_trades == 0
    assert metrics.win_rate == 0.0
    assert metrics.profit_factor is None
    assert metrics.average_r is None
    assert metrics.final_equity == pytest.approx(1000.0)


def test_win_rate_and_pnl_aggregation_match_hand_computed_values():
    trades = [_trade(100.0, 2.0), _trade(-50.0, -1.0), _trade(50.0, 1.0)]
    metrics = compute_metrics(trades, [], initial_equity=1000.0)
    assert metrics.total_trades == 3
    assert metrics.wins == 2
    assert metrics.losses == 1
    assert metrics.win_rate == pytest.approx(2 / 3)
    assert metrics.gross_profit == pytest.approx(150.0)
    assert metrics.gross_loss == pytest.approx(50.0)
    assert metrics.net_pnl == pytest.approx(100.0)
    assert metrics.profit_factor == pytest.approx(3.0)
    assert metrics.expectancy == pytest.approx(100.0 / 3)


def test_profit_factor_none_when_no_losses():
    trades = [_trade(100.0, 2.0), _trade(50.0, 1.0)]
    metrics = compute_metrics(trades, [], initial_equity=1000.0)
    assert metrics.profit_factor is None  # undefined, not infinite or zero


def test_total_fees_sums_every_trade():
    trades = [_trade(10.0, 1.0, fees=2.0), _trade(-5.0, -0.5, fees=1.5)]
    metrics = compute_metrics(trades, [], initial_equity=1000.0)
    assert metrics.total_fees == pytest.approx(3.5)


def test_max_drawdown_matches_hand_computed_value():
    curve = [(_T0, 1000.0), (_T0, 1100.0), (_T0, 900.0), (_T0, 950.0)]
    metrics = compute_metrics([], curve, initial_equity=1000.0)
    # peak=1100, trough=900 -> dd = 200/1100
    assert metrics.max_drawdown_pct == pytest.approx(200 / 1100)


def test_max_drawdown_zero_for_a_monotonically_rising_curve():
    curve = [(_T0, 1000.0), (_T0, 1050.0), (_T0, 1100.0)]
    metrics = compute_metrics([], curve, initial_equity=1000.0)
    assert metrics.max_drawdown_pct == pytest.approx(0.0)


def test_max_consecutive_losses_counts_the_longest_streak_only():
    trades = [_trade(-1, -1), _trade(-1, -1), _trade(10, 1), _trade(-1, -1), _trade(-1, -1), _trade(-1, -1)]
    metrics = compute_metrics(trades, [], initial_equity=1000.0)
    assert metrics.max_consecutive_losses == 3


def test_average_and_median_r_match_hand_computed_values():
    trades = [_trade(1, 1.0), _trade(1, 2.0), _trade(1, 3.0)]
    metrics = compute_metrics(trades, [], initial_equity=1000.0)
    assert metrics.average_r == pytest.approx(2.0)
    assert metrics.median_r == pytest.approx(2.0)


def test_sharpe_r_none_with_fewer_than_two_trades():
    metrics = compute_metrics([_trade(10, 1.0)], [], initial_equity=1000.0)
    assert metrics.sharpe_r is None


def test_sharpe_r_none_when_r_multiples_have_zero_variance():
    trades = [_trade(10, 1.0), _trade(10, 1.0), _trade(10, 1.0)]
    metrics = compute_metrics(trades, [], initial_equity=1000.0)
    assert metrics.sharpe_r is None  # constant series -> undefined, not a lie


def test_sharpe_r_matches_hand_computed_value():
    trades = [_trade(1, 1.0), _trade(1, 3.0)]
    metrics = compute_metrics(trades, [], initial_equity=1000.0)
    # mean=2, population std=1 -> sharpe=2
    assert metrics.sharpe_r == pytest.approx(2.0)


def test_sortino_r_none_without_any_losing_trades():
    trades = [_trade(1, 1.0), _trade(1, 2.0)]
    metrics = compute_metrics(trades, [], initial_equity=1000.0)
    assert metrics.sortino_r is None


def test_total_return_and_final_equity_match_hand_computed_values():
    trades = [_trade(200.0, 2.0)]
    metrics = compute_metrics(trades, [], initial_equity=1000.0)
    assert metrics.final_equity == pytest.approx(1200.0)
    assert metrics.total_return_pct == pytest.approx(20.0)


def test_recovery_factor_none_without_any_drawdown():
    trades = [_trade(100.0, 2.0)]
    metrics = compute_metrics(trades, [], initial_equity=1000.0)
    assert metrics.recovery_factor is None
