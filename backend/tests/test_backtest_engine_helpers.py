from datetime import UTC, datetime

import pandas as pd
import pytest

from aegis.backtest.engine import (
    _apply_slippage,
    _apply_trade_outcome,
    _check_exit,
    _close_trade,
    _compute_stop,
    _compute_take_profit,
)
from aegis.backtest.models import BacktestConfig, OpenPosition
from aegis.risk.service import AccountState

_T0 = datetime(2026, 1, 1, tzinfo=UTC)


def test_apply_slippage_long_entry_pays_more():
    assert _apply_slippage(100.0, "LONG", is_entry=True, slippage_pct=0.001) == pytest.approx(100.1)


def test_apply_slippage_long_exit_receives_less():
    assert _apply_slippage(100.0, "LONG", is_entry=False, slippage_pct=0.001) == pytest.approx(99.9)


def test_apply_slippage_short_entry_pays_less_favorable_too():
    # entering SHORT = selling -> receive slightly less than the raw price
    assert _apply_slippage(100.0, "SHORT", is_entry=True, slippage_pct=0.001) == pytest.approx(99.9)


def test_apply_slippage_short_exit_pays_more():
    # exiting SHORT = buying back -> pay slightly more
    assert _apply_slippage(100.0, "SHORT", is_entry=False, slippage_pct=0.001) == pytest.approx(100.1)


def test_compute_stop_long_is_below_entry():
    assert _compute_stop(100.0, atr=2.0, side="LONG", atr_multiple=2.0) == pytest.approx(96.0)


def test_compute_stop_short_is_above_entry():
    assert _compute_stop(100.0, atr=2.0, side="SHORT", atr_multiple=2.0) == pytest.approx(104.0)


def test_compute_stop_none_when_atr_missing_or_zero():
    assert _compute_stop(100.0, atr=None, side="LONG", atr_multiple=2.0) is None
    assert _compute_stop(100.0, atr=0.0, side="LONG", atr_multiple=2.0) is None


def test_compute_take_profit_matches_r_multiple():
    tp = _compute_take_profit(100.0, stop_price=96.0, side="LONG", r_multiple=2.0)
    assert tp == pytest.approx(108.0)  # risk=4, reward=8 -> 2R


def _position(side="LONG", stop=96.0, tp=108.0) -> OpenPosition:
    return OpenPosition(side=side, entry_time=_T0, entry_price=100.0, stop_price=stop,
                         take_profit_price=tp, quantity=1.0, risk_amount=4.0,
                         confluence_score=50.0, reasons=[])


def test_check_exit_long_hits_stop():
    bar = pd.Series({"high": 101.0, "low": 95.0})
    result = _check_exit(_position(), bar)
    assert result == (96.0, "STOP")


def test_check_exit_long_hits_take_profit():
    bar = pd.Series({"high": 109.0, "low": 99.0})
    result = _check_exit(_position(), bar)
    assert result == (108.0, "TAKE_PROFIT")


def test_check_exit_long_prefers_stop_when_both_in_range():
    bar = pd.Series({"high": 109.0, "low": 95.0})  # both stop and TP within this bar's range
    result = _check_exit(_position(), bar)
    assert result[1] == "STOP"


def test_check_exit_short_hits_stop():
    bar = pd.Series({"high": 105.0, "low": 91.0})
    result = _check_exit(_position(side="SHORT", stop=104.0, tp=92.0), bar)
    assert result == (104.0, "STOP")


def test_check_exit_none_when_neither_hit():
    bar = pd.Series({"high": 102.0, "low": 98.0})
    assert _check_exit(_position(), bar) is None


def test_close_trade_applies_fees_and_slippage():
    config = BacktestConfig(symbol="BTCUSDT", interval="1h", fees_pct=0.001, slippage_pct=0.0)
    position = _position()
    trade = _close_trade(position, _T0, 108.0, "TAKE_PROFIT", config)
    # no slippage here -> exit_price=108, gross=(108-100)*1=8, fees=(100+108)*1*0.001=0.208
    assert trade.exit_price == pytest.approx(108.0)
    assert trade.gross_pnl == pytest.approx(8.0)
    assert trade.fees_paid == pytest.approx(0.208)
    assert trade.net_pnl == pytest.approx(8.0 - 0.208)
    assert trade.r_multiple == pytest.approx(trade.net_pnl / 4.0)


def test_close_trade_short_side_pnl_direction():
    config = BacktestConfig(symbol="BTCUSDT", interval="1h", fees_pct=0.0, slippage_pct=0.0)
    position = _position(side="SHORT", stop=104.0, tp=92.0)
    trade = _close_trade(position, _T0, 92.0, "TAKE_PROFIT", config)
    assert trade.gross_pnl == pytest.approx(8.0)  # entry 100, exit 92, short -> profit


def test_apply_trade_outcome_win_resets_streak_and_raises_peak():
    account = AccountState(equity=1000.0, peak_equity=1000.0, daily_starting_equity=1000.0,
                            daily_realized_pnl=0.0, consecutive_losses=3, open_positions_count=1)
    result = _apply_trade_outcome(account, 50.0)
    assert result.equity == pytest.approx(1050.0)
    assert result.peak_equity == pytest.approx(1050.0)
    assert result.consecutive_losses == 0
    assert result.open_positions_count == 0


def test_apply_trade_outcome_loss_increments_streak_and_keeps_peak():
    account = AccountState(equity=1000.0, peak_equity=1000.0, daily_starting_equity=1000.0,
                            daily_realized_pnl=0.0, consecutive_losses=2, open_positions_count=1)
    result = _apply_trade_outcome(account, -30.0)
    assert result.equity == pytest.approx(970.0)
    assert result.peak_equity == pytest.approx(1000.0)  # unchanged - no new high
    assert result.consecutive_losses == 3
