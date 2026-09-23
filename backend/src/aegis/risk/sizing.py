"""Position sizing - spec section 19.

    risk_amount = account_equity * risk_per_trade
    stop_distance = abs(entry_price - stop_price)
    position_size = risk_amount / stop_distance

...then rounded down to the exchange's step size (never up - rounding up
would silently risk more than `risk_per_trade` says) and checked against
`min_notional`. Reuses `SymbolRules` from the Binance provider (Phase 1) -
tick/step/minNotional are never hard-coded, always read from exchangeInfo.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from typing import Literal

from aegis.providers.binance.models import SymbolRules

SizingStatus = Literal["OK", "NO_STOP_DISTANCE", "ZERO_QUANTITY", "BELOW_MIN_NOTIONAL"]


@dataclass(slots=True)
class PositionSizeResult:
    status: SizingStatus
    quantity: float
    notional: float
    risk_amount: float
    stop_distance: float


def round_down_to_step(value: float, step: float) -> float:
    """Decimal-based, not float math - `math.floor(value/step)*step` drifts
    on binary floats (e.g. step=0.001) in exactly the way you don't want
    when the result is how much money is at risk."""
    if step <= 0:
        return value
    d_value = Decimal(str(value))
    d_step = Decimal(str(step))
    steps = (d_value / d_step).to_integral_value(rounding=ROUND_DOWN)
    return float(steps * d_step)


def calculate_position_size(
    equity: float, risk_per_trade: float, entry_price: float, stop_price: float, symbol_rules: SymbolRules,
) -> PositionSizeResult:
    risk_amount = equity * risk_per_trade
    stop_distance = abs(entry_price - stop_price)

    if stop_distance <= 0:
        return PositionSizeResult("NO_STOP_DISTANCE", 0.0, 0.0, risk_amount, stop_distance)

    raw_quantity = risk_amount / stop_distance
    quantity = round_down_to_step(raw_quantity, symbol_rules.step_size) if symbol_rules.step_size > 0 else raw_quantity

    if quantity <= 0:
        return PositionSizeResult("ZERO_QUANTITY", 0.0, 0.0, risk_amount, stop_distance)

    notional = quantity * entry_price
    if symbol_rules.min_notional > 0 and notional < symbol_rules.min_notional:
        return PositionSizeResult("BELOW_MIN_NOTIONAL", quantity, notional, risk_amount, stop_distance)

    return PositionSizeResult("OK", quantity, notional, risk_amount, stop_distance)
