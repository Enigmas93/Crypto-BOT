"""Shadow Trading data types - spec sections 9, 120.

Shadow Trading places REAL orders on Binance Futures (testnet by default,
gated by `BINANCE_TESTNET`/`LIVE_TRADING`) instead of simulating a fill
internally the way Paper Trading does - the only engine so far where a
position's existence is verified by asking the exchange, not by trusting
local state.

Fees are ESTIMATED via `fees_pct` (same approach as Paper/Backtest), not
fetched from Binance's real commission/trade-history endpoints - a
documented simplification (spec rule 151), not a claim of exact precision.
A future enhancement could pull the real commission from
`/fapi/v1/userTrades`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from aegis.strategy.strategies import ALL_STRATEGY_IDS

Side = str  # "LONG" | "SHORT"


@dataclass(slots=True)
class ShadowTradingConfig:
    symbol: str
    interval: str
    account_id: str = "shadow"
    fees_pct: float = 0.0004  # ESTIMATED - see module docstring
    strategy_ids: tuple[str, ...] = ALL_STRATEGY_IDS
    strategy_weights: dict[str, float] | None = None
    confluence_threshold: float = 30.0
    stop_atr_multiple: float = 2.0
    take_profit_r_multiple: float = 2.0
    leverage: int = 3
    warmup_bars: int = 210
    candle_limit: int = 500


@dataclass(slots=True)
class ShadowPosition:
    """The local record of a real, exchange-side bracket: a filled entry
    plus two protective orders (stop-loss, take-profit) - exactly one of
    which is expected to eventually fill and close the position."""
    side: Side
    entry_time: datetime
    entry_price: float
    quantity: float
    stop_order_id: int
    take_profit_order_id: int
    stop_price: float
    take_profit_price: float
    risk_amount: float
    confluence_score: float
    reasons: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ShadowTrade:
    account_id: str
    symbol: str
    side: Side
    entry_time: datetime
    entry_price: float
    exit_time: datetime
    exit_price: float
    exit_reason: str  # STOP | TAKE_PROFIT | RECONCILIATION_FAILED
    quantity: float
    gross_pnl: float
    fees_paid: float
    net_pnl: float
    r_multiple: float
    confluence_score: float
    reasons: list[str] = field(default_factory=list)
