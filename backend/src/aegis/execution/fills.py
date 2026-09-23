"""Pure fill/exit simulation, shared by BacktestEngine (Phase 8) and
PaperTradingEngine (Phase 9) - spec section 120: "a única diferença entre
paper/live/backtest deve ser o ExecutionProvider". This module is that
shared core: no I/O, no knowledge of which mode is running it.

Moved out of `aegis.backtest.engine` (where it was originally written and
tested) once a second, independent consumer (Paper Trading) needed the
exact same logic - duplicating it would have created the one thing spec
section 120 explicitly warns against: two engines that quietly drift apart
on how a fill actually gets computed.
"""
from __future__ import annotations

import pandas as pd

from aegis.execution.models import FillOutcome, OpenPosition


def apply_slippage(price: float, side: str, is_entry: bool, slippage_pct: float) -> float:
    """Entering LONG or exiting SHORT means buying (pay a bit more);
    entering SHORT or exiting LONG means selling (receive a bit less)."""
    buying = (side == "LONG") == is_entry
    return price * (1 + slippage_pct) if buying else price * (1 - slippage_pct)


def compute_stop(entry_price: float, atr: float | None, side: str, atr_multiple: float) -> float | None:
    if atr is None or atr <= 0:
        return None
    distance = atr * atr_multiple
    return entry_price - distance if side == "LONG" else entry_price + distance


def compute_take_profit(entry_price: float, stop_price: float, side: str, r_multiple: float) -> float:
    stop_distance = abs(entry_price - stop_price)
    return entry_price + stop_distance * r_multiple if side == "LONG" else entry_price - stop_distance * r_multiple


def check_exit(position: OpenPosition, bar: pd.Series) -> tuple[float, str] | None:
    """Stop checked before take-profit when a single bar's range could
    plausibly hit both - the conservative assumption (spec section 82:
    risk beats profit when the two conflict), since intrabar tick order
    is unknowable from OHLC data alone."""
    if position.side == "LONG":
        if bar["low"] <= position.stop_price:
            return position.stop_price, "STOP"
        if bar["high"] >= position.take_profit_price:
            return position.take_profit_price, "TAKE_PROFIT"
    else:
        if bar["high"] >= position.stop_price:
            return position.stop_price, "STOP"
        if bar["low"] <= position.take_profit_price:
            return position.take_profit_price, "TAKE_PROFIT"
    return None


def close_position(
    position: OpenPosition, raw_exit_price: float, exit_reason: str, fees_pct: float, slippage_pct: float,
) -> FillOutcome:
    exit_price = apply_slippage(raw_exit_price, position.side, is_entry=False, slippage_pct=slippage_pct)
    fees_paid = (position.entry_price + exit_price) * position.quantity * fees_pct

    if position.side == "LONG":
        gross_pnl = (exit_price - position.entry_price) * position.quantity
    else:
        gross_pnl = (position.entry_price - exit_price) * position.quantity
    net_pnl = gross_pnl - fees_paid
    r_multiple = net_pnl / position.risk_amount if position.risk_amount > 0 else 0.0

    return FillOutcome(
        exit_price=exit_price, exit_reason=exit_reason, gross_pnl=gross_pnl,
        fees_paid=fees_paid, net_pnl=net_pnl, r_multiple=r_multiple,
    )
