"""Pure risk rules - spec sections 20-23, 56, 112.

Every function here takes plain numbers and returns a plain result: no
account object, no database, no side effects. `RiskEngine` (service.py) is
the only thing that wires these to real account state.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

DrawdownState = Literal["NORMAL", "CAUTION", "REDUCED_RISK", "HALTED"]
LossStreakAction = Literal["NONE", "COOLDOWN", "REDUCE_RISK", "HALT"]


def compute_drawdown_pct(current_equity: float, peak_equity: float) -> float:
    if peak_equity <= 0:
        return 0.0
    return max(0.0, (peak_equity - current_equity) / peak_equity)


def compute_drawdown_state(
    current_equity: float, peak_equity: float,
    caution_pct: float, reduced_risk_pct: float, halted_pct: float,
) -> DrawdownState:
    """spec section 21: NORMAL -> CAUTION -> REDUCED_RISK -> HALTED, each
    threshold measured against the account's own peak equity (a new
    all-time-high always starts everyone back at NORMAL)."""
    dd = compute_drawdown_pct(current_equity, peak_equity)
    if dd >= halted_pct:
        return "HALTED"
    if dd >= reduced_risk_pct:
        return "REDUCED_RISK"
    if dd >= caution_pct:
        return "CAUTION"
    return "NORMAL"


def loss_streak_action(
    consecutive_losses: int, cooldown_threshold: int, reduce_risk_threshold: int, halt_threshold: int,
) -> LossStreakAction:
    """spec section 22: 3 losses -> cooldown, 5 -> reduce risk, past the
    configured ceiling -> halt. Thresholds are configurable, never assumed
    fixed."""
    if consecutive_losses >= halt_threshold:
        return "HALT"
    if consecutive_losses >= reduce_risk_threshold:
        return "REDUCE_RISK"
    if consecutive_losses >= cooldown_threshold:
        return "COOLDOWN"
    return "NONE"


def compute_net_r_multiple(
    entry_price: float, stop_price: float, take_profit_price: float,
    fees_pct: float = 0.0008, slippage_pct: float = 0.0005,
) -> float | None:
    """spec section 56: reward:risk after fees/slippage, not the naive
    gross ratio. Returns None when the stop distance is zero (undefined)."""
    stop_distance = abs(entry_price - stop_price)
    if stop_distance == 0:
        return None
    reward_distance = abs(take_profit_price - entry_price)
    cost = entry_price * (fees_pct + slippage_pct)
    net_reward = max(0.0, reward_distance - cost)
    net_risk = stop_distance + cost
    return net_reward / net_risk


@dataclass(slots=True)
class LiquidationCheck:
    stop_distance_pct: float
    estimated_liquidation_distance_pct: float
    safe: bool


def estimate_liquidation_distance_pct(leverage: int, maintenance_margin_rate: float) -> float:
    """Approximation for isolated margin: distance from entry to estimated
    liquidation, as a fraction of entry price. `1/leverage` is the raw
    margin cushion; maintenance margin eats into it before liquidation
    actually triggers. This is deliberately conservative and NOT Binance's
    real tiered maintenance-margin table (that needs an authenticated
    `/fapi/v1/leverageBracket` call, notional-bracket dependent) - a
    documented approximation, not a claim of precision (spec rule 151)."""
    if leverage <= 0:
        raise ValueError("leverage must be positive")
    return max(0.0, (1.0 / leverage) - maintenance_margin_rate)


def check_liquidation_distance(
    entry_price: float, stop_price: float, leverage: int,
    maintenance_margin_rate: float, safety_margin: float,
) -> LiquidationCheck:
    """spec section 112: never accept a trade whose stop sits so close to
    the estimated liquidation price that liquidation could plausibly
    trigger before the stop does. `safety_margin` (e.g. 0.75) requires the
    stop to sit within that fraction of the estimated liquidation
    distance - a buffer, not a hard boundary."""
    stop_distance_pct = abs(entry_price - stop_price) / entry_price if entry_price else 0.0
    liq_distance_pct = estimate_liquidation_distance_pct(leverage, maintenance_margin_rate)
    safe = stop_distance_pct <= liq_distance_pct * safety_margin
    return LiquidationCheck(
        stop_distance_pct=stop_distance_pct,
        estimated_liquidation_distance_pct=liq_distance_pct,
        safe=safe,
    )
