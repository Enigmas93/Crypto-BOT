"""RiskEngine - spec sections 17, 20-23, 56, 110-112.

The one authority with veto power over every other engine (spec section
17: "Signal → Risk Check → Position Sizing → Execution", never
"Signal → Execution"). `evaluate()` collects every applicable block reason
rather than stopping at the first one - a blocked trade's audit trail
should say everything that was wrong with it, not just whichever check
happened to run first.

No strategy/signal engine exists yet in this codebase (that's future
work), so `TradeProposal` is a deliberately generic "here is a
hypothetical trade" input - this engine's job is to evaluate whatever
proposal it's handed, regardless of what produced it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from aegis.providers.binance.models import SymbolRules
from aegis.risk.rules import (
    LiquidationCheck,
    check_liquidation_distance,
    compute_drawdown_pct,
    compute_drawdown_state,
    compute_net_r_multiple,
    loss_streak_action,
)
from aegis.risk.sizing import PositionSizeResult, calculate_position_size

Side = Literal["LONG", "SHORT"]


@dataclass(slots=True)
class TradeProposal:
    symbol: str
    side: Side
    entry_price: float
    stop_price: float
    take_profit_price: float | None = None
    leverage: int = 1
    strategy_id: str | None = None


@dataclass(slots=True)
class AccountState:
    equity: float
    peak_equity: float
    daily_starting_equity: float
    daily_realized_pnl: float  # negative = a losing day so far
    consecutive_losses: int
    open_positions_count: int
    correlated_exposure_pct: float = 0.0


@dataclass(slots=True)
class RiskDecision:
    decision: Literal["PASS", "BLOCK"]
    reasons: list[str]
    drawdown_state: str
    drawdown_pct: float
    loss_streak_action: str
    effective_risk_per_trade: float
    position_size: PositionSizeResult
    net_r_multiple: float | None
    liquidation_check: LiquidationCheck


class RiskEngine:
    def __init__(self, settings) -> None:
        self.settings = settings

    def _effective_risk_factor(self, drawdown_state: str, streak_action: str) -> float:
        factors = [1.0]
        if drawdown_state == "CAUTION":
            factors.append(self.settings.risk_reduction_factor_caution)
        elif drawdown_state == "REDUCED_RISK":
            factors.append(self.settings.risk_reduction_factor_reduced)
        if streak_action == "REDUCE_RISK":
            factors.append(self.settings.risk_reduction_factor_loss_streak)
        return min(factors)

    def evaluate(self, proposal: TradeProposal, account: AccountState, symbol_rules: SymbolRules) -> RiskDecision:
        s = self.settings
        reasons: list[str] = []

        # -- stop-loss must exist and be real (spec section 110) --------------
        if proposal.stop_price == proposal.entry_price:
            reasons.append("NO_STOP")

        # -- drawdown guard (spec section 21) ----------------------------------
        drawdown_pct = compute_drawdown_pct(account.equity, account.peak_equity)
        drawdown_state = compute_drawdown_state(
            account.equity, account.peak_equity,
            s.drawdown_caution_pct, s.drawdown_reduced_risk_pct, s.max_drawdown,
        )
        if drawdown_state == "HALTED":
            reasons.append("DRAWDOWN_LIMIT")

        # -- daily loss limit (spec section 20) --------------------------------
        daily_loss_pct = 0.0
        if account.daily_realized_pnl < 0 and account.daily_starting_equity > 0:
            daily_loss_pct = -account.daily_realized_pnl / account.daily_starting_equity
        if daily_loss_pct >= s.max_daily_loss:
            reasons.append("DAILY_LOSS_LIMIT")

        # -- consecutive-loss streak guard (spec section 22) -------------------
        streak_action = loss_streak_action(
            account.consecutive_losses, s.loss_streak_cooldown_threshold,
            s.loss_streak_reduce_risk_threshold, s.loss_streak_halt_threshold,
        )
        if streak_action == "HALT":
            reasons.append("LOSS_STREAK_HALT")

        # -- exposure limits (spec section 110) --------------------------------
        if account.open_positions_count >= s.max_open_positions:
            reasons.append("MAX_POSITIONS")
        if account.correlated_exposure_pct >= s.max_correlated_exposure_pct:
            reasons.append("CORRELATED_EXPOSURE")

        # -- leverage cap (spec section 111) -----------------------------------
        if proposal.leverage > s.max_leverage:
            reasons.append("LEVERAGE_TOO_HIGH")

        # -- risk/reward after costs (spec section 56) -------------------------
        net_r_multiple: float | None = None
        if proposal.take_profit_price is not None:
            net_r_multiple = compute_net_r_multiple(
                proposal.entry_price, proposal.stop_price, proposal.take_profit_price,
            )
            if net_r_multiple is not None and net_r_multiple < s.min_r_multiple:
                reasons.append("BAD_RISK_REWARD")

        # -- liquidation distance (spec section 112) ---------------------------
        liquidation_check = check_liquidation_distance(
            proposal.entry_price, proposal.stop_price, proposal.leverage,
            s.maintenance_margin_rate_estimate, s.liquidation_safety_margin,
        )
        if not liquidation_check.safe:
            reasons.append("LIQUIDATION_TOO_CLOSE")

        # -- position sizing, at whatever risk this account state allows now --
        effective_factor = self._effective_risk_factor(drawdown_state, streak_action)
        effective_risk_per_trade = s.risk_per_trade * effective_factor
        position_size = calculate_position_size(
            account.equity, effective_risk_per_trade, proposal.entry_price, proposal.stop_price, symbol_rules,
        )
        if position_size.status != "OK":
            reasons.append(position_size.status)

        return RiskDecision(
            decision="BLOCK" if reasons else "PASS",
            reasons=reasons,
            drawdown_state=drawdown_state,
            drawdown_pct=drawdown_pct,
            loss_streak_action=streak_action,
            effective_risk_per_trade=effective_risk_per_trade,
            position_size=position_size,
            net_r_multiple=net_r_multiple,
            liquidation_check=liquidation_check,
        )
