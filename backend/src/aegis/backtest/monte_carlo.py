"""Monte Carlo resampling over a backtest's trade sequence.

A single backtest is one specific ORDER of trades through one specific
slice of history - its equity curve and max drawdown are as much a product
of luck (which trades happened to come first, cluster together, etc.) as
of the strategy's actual edge. Bootstrap-resampling the same trades (with
replacement, many times) into alternate orderings answers a different,
more useful question: if this same win/loss distribution kept playing out,
how bad could the drawdown realistically get, and how wide is the range of
plausible outcomes - not just the one path that happened to occur.

Deliberately reuses each trade's actual `net_pnl` (not a re-derived
R-multiple times a fixed risk amount) - net_pnl already reflects the real
position sizing, fees and slippage that specific trade experienced, so
resampling it makes no new assumption about what a trade "should" have
returned.

This does NOT re-simulate the strategy or touch market data - it is purely
a statistical resampling of a `BacktestResult` that already exists.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from aegis.backtest.models import BacktestTrade


@dataclass(slots=True)
class MonteCarloResult:
    num_simulations: int
    num_trades: int
    initial_equity: float
    final_equity_p5: float
    final_equity_p25: float
    final_equity_p50: float
    final_equity_p75: float
    final_equity_p95: float
    max_drawdown_pct_p50: float
    max_drawdown_pct_p95: float  # a realistically bad case, not the tail extreme
    max_drawdown_pct_worst: float  # worst of every simulated path
    probability_of_ruin: float  # fraction of paths where equity ever hit <= 0


def _max_drawdown_pct(equity_path: np.ndarray, initial_equity: float) -> float:
    running_peak = np.maximum.accumulate(np.concatenate(([initial_equity], equity_path)))[1:]
    drawdowns = np.where(running_peak > 0, (running_peak - equity_path) / running_peak, 0.0)
    return float(drawdowns.max()) if len(drawdowns) else 0.0


def run_monte_carlo(
    trades: list[BacktestTrade], initial_equity: float, num_simulations: int = 2000, seed: int | None = None,
) -> MonteCarloResult:
    if not trades:
        raise ValueError("cannot run a Monte Carlo simulation with zero trades - run a backtest first")
    if num_simulations < 1:
        raise ValueError(f"num_simulations must be >= 1, got {num_simulations}")

    net_pnls = np.array([t.net_pnl for t in trades], dtype=float)
    n = len(net_pnls)
    rng = np.random.default_rng(seed)

    final_equities = np.empty(num_simulations)
    max_drawdowns = np.empty(num_simulations)
    ruined = np.empty(num_simulations, dtype=bool)

    for i in range(num_simulations):
        sample = rng.choice(net_pnls, size=n, replace=True)
        equity_path = initial_equity + np.cumsum(sample)
        final_equities[i] = equity_path[-1]
        max_drawdowns[i] = _max_drawdown_pct(equity_path, initial_equity)
        ruined[i] = bool(equity_path.min() <= 0)

    return MonteCarloResult(
        num_simulations=num_simulations,
        num_trades=n,
        initial_equity=initial_equity,
        final_equity_p5=float(np.percentile(final_equities, 5)),
        final_equity_p25=float(np.percentile(final_equities, 25)),
        final_equity_p50=float(np.percentile(final_equities, 50)),
        final_equity_p75=float(np.percentile(final_equities, 75)),
        final_equity_p95=float(np.percentile(final_equities, 95)),
        max_drawdown_pct_p50=float(np.percentile(max_drawdowns, 50)),
        max_drawdown_pct_p95=float(np.percentile(max_drawdowns, 95)),
        max_drawdown_pct_worst=float(max_drawdowns.max()),
        probability_of_ruin=float(ruined.mean()),
    )
