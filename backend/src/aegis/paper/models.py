"""Paper Trading data types - spec section 9 (Modos & Controle) and 120.

Deliberately a separate, independently-declared config from
`BacktestConfig` rather than a shared base class - the two engines have
genuinely different lifecycles (one seeds a fresh in-memory account and
replays a fixed historical window once; the other runs forever against a
real, persisted account and only ever looks at "now"). The strategy/
execution parameters they share are simple data fields, not logic - the
logic that actually needs to be identical (fill/exit math) already is,
via `aegis.execution.fills`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from aegis.strategy.strategies import ALL_STRATEGY_IDS

Side = str  # "LONG" | "SHORT"


@dataclass(slots=True)
class PaperTradingConfig:
    symbol: str
    interval: str
    account_id: str = "paper"
    # Binance USDT-M taker fee is 0.04-0.05% depending on VIP tier - 0.04%
    # (the base retail taker rate) is the honest default, not a guess.
    fees_pct: float = 0.0004
    slippage_pct: float = 0.0005
    strategy_ids: tuple[str, ...] = ALL_STRATEGY_IDS
    strategy_weights: dict[str, float] | None = None
    confluence_threshold: float = 30.0
    stop_atr_multiple: float = 2.0
    take_profit_r_multiple: float = 2.0
    leverage: int = 3
    warmup_bars: int = 210
    candle_limit: int = 500


@dataclass(slots=True)
class PaperTrade:
    account_id: str
    symbol: str
    side: Side
    entry_time: datetime
    entry_price: float
    exit_time: datetime
    exit_price: float
    exit_reason: str  # STOP | TAKE_PROFIT
    quantity: float
    gross_pnl: float
    fees_paid: float
    net_pnl: float
    r_multiple: float
    confluence_score: float
    reasons: list[str] = field(default_factory=list)
