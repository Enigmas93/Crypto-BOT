"""Backtest data types - spec sections 71/74."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from aegis.execution.models import OpenPosition, Side  # noqa: F401 - re-exported, see below
from aegis.strategy.strategies import ALL_STRATEGY_IDS

# OpenPosition moved to aegis.execution.models (Phase 9) so PaperTradingEngine
# can share it too - re-exported here so `from aegis.backtest.models import
# OpenPosition` (existing code, existing tests) keeps working unchanged.


@dataclass(slots=True)
class BacktestConfig:
    symbol: str
    interval: str
    initial_equity: float = 1000.0
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
    warmup_bars: int = 210  # bars needed before compute_snapshot reaches quality=OK (EMA200 etc.)
    risk_account_id: str = "backtest"


@dataclass(slots=True)
class BacktestTrade:
    symbol: str
    side: Side
    entry_time: datetime
    entry_price: float
    exit_time: datetime
    exit_price: float
    exit_reason: str  # STOP | TAKE_PROFIT | END_OF_DATA
    quantity: float
    gross_pnl: float
    fees_paid: float
    net_pnl: float
    r_multiple: float
    confluence_score: float
    reasons: list[str] = field(default_factory=list)


@dataclass(slots=True)
class BacktestResult:
    config: BacktestConfig
    trades: list[BacktestTrade]
    equity_curve: list[tuple[datetime, float]]
    # `BacktestMetrics` lives in metrics.py, which imports `BacktestTrade`
    # from this module - a string annotation (safe under `from __future__
    # import annotations`) avoids a real circular import for what is, at
    # runtime, just a type hint.
    metrics: "BacktestMetrics"
    # The actual candle range the run consumed (including warmup bars) -
    # recorded so a persisted run says exactly what data it covered,
    # without needing to re-fetch and re-run to find out.
    data_start: datetime
    data_end: datetime
    total_bars: int
    # In-memory kill switch simulation (spec section 23) - see engine.py's
    # module docstring for why this never touches the real, persisted
    # KillSwitchRepository. True here means the run stopped opening new
    # positions before it reached the end of the data.
    kill_switch_triggered: bool = False
    kill_switch_reasons: list[str] = field(default_factory=list)
    kill_switch_tripped_at: datetime | None = None
