"""Backtest performance metrics - spec section 72.

`sharpe`/`sortino`/`calmar` here are computed over the per-trade R-multiple
series, not annualized time-series returns - the classic definitions need
a calendar-aware annualization factor that depends on bar frequency and
trading calendar assumptions this module has no business guessing at.
Documented as a deliberate simplification, not hidden as if it were the
textbook formula.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from aegis.backtest.models import BacktestTrade


@dataclass(slots=True)
class BacktestMetrics:
    total_trades: int
    wins: int
    losses: int
    win_rate: float
    loss_rate: float
    gross_profit: float
    gross_loss: float
    net_pnl: float
    profit_factor: float | None
    average_win: float
    average_loss: float
    expectancy: float
    total_fees: float
    max_drawdown_pct: float
    recovery_factor: float | None
    average_r: float | None
    median_r: float | None
    sharpe_r: float | None
    sortino_r: float | None
    calmar_r: float | None
    max_consecutive_losses: int
    total_return_pct: float
    final_equity: float


def _max_drawdown_pct(equity_curve: list[tuple], initial_equity: float) -> float:
    if not equity_curve:
        return 0.0
    values = np.array([e for _, e in equity_curve], dtype=float)
    running_peak = np.maximum.accumulate(np.concatenate(([initial_equity], values)))[1:]
    drawdowns = np.where(running_peak > 0, (running_peak - values) / running_peak, 0.0)
    return float(drawdowns.max()) if len(drawdowns) else 0.0


def _max_consecutive_losses(trades: list[BacktestTrade]) -> int:
    streak = 0
    worst = 0
    for t in trades:
        if t.net_pnl < 0:
            streak += 1
            worst = max(worst, streak)
        else:
            streak = 0
    return worst


def compute_metrics(
    trades: list[BacktestTrade], equity_curve: list[tuple], initial_equity: float,
) -> BacktestMetrics:
    total_trades = len(trades)
    wins = [t for t in trades if t.net_pnl > 0]
    losses = [t for t in trades if t.net_pnl < 0]

    gross_profit = sum(t.net_pnl for t in wins)
    gross_loss = abs(sum(t.net_pnl for t in losses))
    net_pnl = sum(t.net_pnl for t in trades)
    total_fees = sum(t.fees_paid for t in trades)

    win_rate = len(wins) / total_trades if total_trades else 0.0
    loss_rate = len(losses) / total_trades if total_trades else 0.0
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else None
    average_win = gross_profit / len(wins) if wins else 0.0
    average_loss = gross_loss / len(losses) if losses else 0.0
    expectancy = net_pnl / total_trades if total_trades else 0.0

    max_dd = _max_drawdown_pct(equity_curve, initial_equity)
    recovery_factor = (net_pnl / (max_dd * initial_equity)) if max_dd > 0 else None

    r_multiples = np.array([t.r_multiple for t in trades], dtype=float) if trades else np.array([])
    average_r = float(r_multiples.mean()) if len(r_multiples) else None
    median_r = float(np.median(r_multiples)) if len(r_multiples) else None

    sharpe_r = None
    sortino_r = None
    if len(r_multiples) >= 2:
        std_r = float(r_multiples.std(ddof=0))
        if std_r > 0:
            sharpe_r = float(r_multiples.mean() / std_r)
        downside = r_multiples[r_multiples < 0]
        if len(downside) > 0:
            downside_std = float(downside.std(ddof=0))
            if downside_std > 0:
                sortino_r = float(r_multiples.mean() / downside_std)

    calmar_r = (average_r / max_dd) if (average_r is not None and max_dd > 0) else None

    final_equity = initial_equity + net_pnl
    total_return_pct = (final_equity - initial_equity) / initial_equity * 100 if initial_equity else 0.0

    return BacktestMetrics(
        total_trades=total_trades, wins=len(wins), losses=len(losses),
        win_rate=win_rate, loss_rate=loss_rate,
        gross_profit=gross_profit, gross_loss=gross_loss, net_pnl=net_pnl,
        profit_factor=profit_factor, average_win=average_win, average_loss=average_loss,
        expectancy=expectancy, total_fees=total_fees,
        max_drawdown_pct=max_dd, recovery_factor=recovery_factor,
        average_r=average_r, median_r=median_r,
        sharpe_r=sharpe_r, sortino_r=sortino_r, calmar_r=calmar_r,
        max_consecutive_losses=_max_consecutive_losses(trades),
        total_return_pct=total_return_pct, final_equity=final_equity,
    )
