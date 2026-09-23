"""Shared position/fill types - spec section 120: the only difference
between backtest, paper and live should be the ExecutionProvider, so the
position-tracking type both `BacktestEngine` (Phase 8) and
`PaperTradingEngine` (Phase 9) hold in memory lives here once.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

Side = str  # "LONG" | "SHORT"


@dataclass(slots=True)
class OpenPosition:
    side: Side
    entry_time: datetime
    entry_price: float
    stop_price: float
    take_profit_price: float
    quantity: float
    risk_amount: float
    confluence_score: float
    reasons: list[str] = field(default_factory=list)


@dataclass(slots=True)
class FillOutcome:
    """The result of closing a position, independent of whatever record
    type a specific engine wraps it in (BacktestTrade, PaperTrade, ...)."""
    exit_price: float
    exit_reason: str
    gross_pnl: float
    fees_paid: float
    net_pnl: float
    r_multiple: float
