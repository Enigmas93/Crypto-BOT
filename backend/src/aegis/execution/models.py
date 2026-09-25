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


class BracketOpenError(RuntimeError):
    """Raised when a bracket could not be fully established. `flattened`
    says whether the emergency close succeeded (True) or also failed
    (False - manual intervention is required immediately).

    Shared across BinanceExecutionProvider and BingXExecutionProvider (Fase
    17d) - each used to define its OWN separate `BracketOpenError` class
    with an identical shape ("Same shape as..." said so in a comment), a
    real production bug: `ShadowTradingEngine`/`MomentumTradingEngine` only
    ever imported Binance's copy, so `except BracketOpenError` there never
    matched the exception BingXExecutionProvider actually raised. Confirmed
    live 2026-09-25 from a Telegram crash report: BingX rejected a stop
    price (code 110411), the entry was correctly flattened, but the
    resulting BracketOpenError propagated uncaught all the way to
    `asyncio.run()` and crashed the whole `run_bingx_shadow_trading.py`
    process - the safety property (never leave a naked position) held, but
    the process itself shouldn't have died over a single symbol's bracket
    failure. One shared class instead of two look-alikes makes this
    mismatch structurally impossible going forward."""

    def __init__(self, message: str, flattened: bool) -> None:
        super().__init__(message)
        self.flattened = flattened
