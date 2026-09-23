"""Direct tests for aegis.execution.fills - the shared fill/exit math used
by both BacktestEngine (Phase 8) and PaperTradingEngine (Phase 9). Tested
here independently of either engine, since this module has no knowledge of
which one is calling it.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pandas as pd
import pytest

from aegis.execution.fills import apply_slippage, check_exit, close_position, compute_stop, compute_take_profit
from aegis.execution.models import OpenPosition

_T0 = datetime(2026, 1, 1, tzinfo=UTC)


def test_apply_slippage_long_entry_pays_more():
    assert apply_slippage(100.0, "LONG", is_entry=True, slippage_pct=0.001) == pytest.approx(100.1)


def test_apply_slippage_long_exit_receives_less():
    assert apply_slippage(100.0, "LONG", is_entry=False, slippage_pct=0.001) == pytest.approx(99.9)


def test_apply_slippage_short_entry_pays_less_favorable_too():
    assert apply_slippage(100.0, "SHORT", is_entry=True, slippage_pct=0.001) == pytest.approx(99.9)


def test_apply_slippage_short_exit_pays_more():
    assert apply_slippage(100.0, "SHORT", is_entry=False, slippage_pct=0.001) == pytest.approx(100.1)


def test_compute_stop_long_is_below_entry():
    assert compute_stop(100.0, atr=2.0, side="LONG", atr_multiple=2.0) == pytest.approx(96.0)


def test_compute_stop_short_is_above_entry():
    assert compute_stop(100.0, atr=2.0, side="SHORT", atr_multiple=2.0) == pytest.approx(104.0)


def test_compute_stop_none_when_atr_missing_or_zero():
    assert compute_stop(100.0, atr=None, side="LONG", atr_multiple=2.0) is None
    assert compute_stop(100.0, atr=0.0, side="LONG", atr_multiple=2.0) is None


def test_compute_take_profit_matches_r_multiple():
    tp = compute_take_profit(100.0, stop_price=96.0, side="LONG", r_multiple=2.0)
    assert tp == pytest.approx(108.0)


def _position(side="LONG", stop=96.0, tp=108.0) -> OpenPosition:
    return OpenPosition(side=side, entry_time=_T0, entry_price=100.0, stop_price=stop,
                         take_profit_price=tp, quantity=1.0, risk_amount=4.0,
                         confluence_score=50.0, reasons=[])


def test_check_exit_long_hits_stop():
    bar = pd.Series({"high": 101.0, "low": 95.0})
    assert check_exit(_position(), bar) == (96.0, "STOP")


def test_check_exit_long_hits_take_profit():
    bar = pd.Series({"high": 109.0, "low": 99.0})
    assert check_exit(_position(), bar) == (108.0, "TAKE_PROFIT")


def test_check_exit_long_prefers_stop_when_both_in_range():
    bar = pd.Series({"high": 109.0, "low": 95.0})
    assert check_exit(_position(), bar)[1] == "STOP"


def test_check_exit_short_hits_stop():
    bar = pd.Series({"high": 105.0, "low": 91.0})
    assert check_exit(_position(side="SHORT", stop=104.0, tp=92.0), bar) == (104.0, "STOP")


def test_check_exit_none_when_neither_hit():
    bar = pd.Series({"high": 102.0, "low": 98.0})
    assert check_exit(_position(), bar) is None


def test_close_position_applies_fees_and_slippage():
    position = _position()
    outcome = close_position(position, raw_exit_price=108.0, exit_reason="TAKE_PROFIT",
                              fees_pct=0.001, slippage_pct=0.0)
    assert outcome.exit_price == pytest.approx(108.0)
    assert outcome.gross_pnl == pytest.approx(8.0)
    assert outcome.fees_paid == pytest.approx(0.208)
    assert outcome.net_pnl == pytest.approx(8.0 - 0.208)
    assert outcome.r_multiple == pytest.approx(outcome.net_pnl / 4.0)
    assert outcome.exit_reason == "TAKE_PROFIT"


def test_close_position_short_side_pnl_direction():
    position = _position(side="SHORT", stop=104.0, tp=92.0)
    outcome = close_position(position, raw_exit_price=92.0, exit_reason="TAKE_PROFIT",
                              fees_pct=0.0, slippage_pct=0.0)
    assert outcome.gross_pnl == pytest.approx(8.0)


def test_close_position_zero_risk_amount_gives_zero_r_multiple_not_a_crash():
    position = _position()
    position.risk_amount = 0.0
    outcome = close_position(position, raw_exit_price=108.0, exit_reason="TAKE_PROFIT",
                              fees_pct=0.0, slippage_pct=0.0)
    assert outcome.r_multiple == 0.0
