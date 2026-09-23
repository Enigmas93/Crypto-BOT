"""Global Kill Switch - spec section 23.

Distinct from the graduated drawdown/loss-streak guards `RiskEngine`
already applies per-proposal (`rules.py`): those recompute fresh on every
`evaluate()` call, so a HALTED drawdown state can quietly become NORMAL
again the moment equity climbs back above the threshold (e.g. an open
position's mark-to-market recovers). A kill switch is meant to be the
opposite of that - once tripped by something serious enough (the account
has genuinely blown through its max drawdown, or has lost so many trades
in a row that something is very likely wrong with the strategy or the
market regime), it must stay tripped until a human looks at it and resets
it explicitly. That stickiness is the whole point, so it lives as its own
persisted state (`KillSwitchRepository`), not as another RiskEngine field.

Deliberately narrow trigger set: `DAILY_LOSS_LIMIT` is excluded on purpose
- it's a self-healing "pause for today" mechanism (spec section 20;
`reset_daily()` clears it every day on its own), not a "something is
fundamentally wrong" signal. Mixing the two would make the kill switch
trip constantly on ordinary bad days, defeating its purpose as a rare,
serious circuit breaker.
"""
from __future__ import annotations

from aegis.risk.rules import compute_drawdown_pct

KILL_SWITCH_MAX_DRAWDOWN = "MAX_DRAWDOWN_BREACHED"
KILL_SWITCH_LOSS_STREAK = "LOSS_STREAK_HALT_THRESHOLD"


def evaluate_kill_switch_triggers(
    equity: float, peak_equity: float, consecutive_losses: int,
    max_drawdown: float, loss_streak_halt_threshold: int,
) -> list[str]:
    """Pure - no account object, no database, no side effects. Returns
    every trigger that currently applies (never just the first), same
    accumulate-don't-short-circuit discipline as `RiskEngine.evaluate()`,
    for the same audit-trail reason (spec section 85)."""
    reasons: list[str] = []
    if compute_drawdown_pct(equity, peak_equity) >= max_drawdown:
        reasons.append(KILL_SWITCH_MAX_DRAWDOWN)
    if consecutive_losses >= loss_streak_halt_threshold:
        reasons.append(KILL_SWITCH_LOSS_STREAK)
    return reasons
