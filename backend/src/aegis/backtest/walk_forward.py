"""WalkForwardEngine (Fase 17) - the validation step the blueprint requires
before LIVE is ever allowed for a strategy: "LIVE só é liberado depois que
a mesma estratégia... passou por backtest aprovado, WALK-FORWARD aprovado,
paper trading aprovado e shadow trading aprovado." `BacktestEngine`
(Phase 8) already existed but only ever ran ONE pass over one fixed
historical window - nothing checked whether a strategy's edge holds up
across DIFFERENT periods, which is the actual defense against a single
lucky (or unlucky) backtest window and the entire point of walk-forward.

Methodology, explicitly scoped (a documented HYPOTHESIS-testing tool, not a
parameter optimizer - spec section 54's own standard: rules/weights are
validated, not auto-tuned): splits one long historical series into
sequential, equal-sized, non-overlapping-by-default folds (`window_bars`
wide, rolling forward `step_bars` each time - `step_bars < window_bars`
means adjacent folds overlap). The SAME fixed `BacktestConfig` (strategy
ids, weights, thresholds) runs unchanged against every fold via
`BacktestEngine.run_over_dataframe` - nothing here refits or re-optimizes
parameters per fold, because no parameter-fitting infrastructure exists
yet anywhere in this codebase (confluence weights are fixed hypotheses
everywhere else too). What this DOES validate: whether a strategy's
performance is consistent across market regimes/periods, or concentrated
in one window that a plain single-pass backtest would never reveal as
lucky. This is a real, named technique ("walk-forward without
re-optimization" / rolling out-of-sample validation), not a watered-down
imitation of the re-optimizing kind - which stays out of scope until this
codebase has something to refit in the first place.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from aegis.backtest.engine import BacktestEngine
from aegis.backtest.models import BacktestConfig, BacktestResult
from aegis.providers.binance.models import SymbolRules


@dataclass(slots=True)
class WalkForwardFold:
    fold_index: int
    window_start: datetime
    window_end: datetime
    result: BacktestResult


@dataclass(slots=True)
class WalkForwardResult:
    symbol: str
    interval: str
    window_bars: int
    step_bars: int
    folds: list[WalkForwardFold]
    profitable_fold_pct: float  # fraction of folds with net_pnl > 0 - the key "is this consistent" number
    fold_net_pnls: list[float]
    fold_win_rates: list[float]
    average_net_pnl: float
    worst_fold_net_pnl: float
    any_fold_kill_switch_triggered: bool


def _fold_metrics(folds: list[WalkForwardFold]) -> WalkForwardResult | None:
    if not folds:
        return None
    net_pnls = [f.result.metrics.net_pnl for f in folds]
    win_rates = [f.result.metrics.win_rate for f in folds]
    profitable = sum(1 for pnl in net_pnls if pnl > 0)
    return WalkForwardResult(
        symbol=folds[0].result.config.symbol, interval=folds[0].result.config.interval,
        window_bars=0, step_bars=0,  # filled in by the caller, which knows these
        folds=folds,
        profitable_fold_pct=round(profitable / len(folds), 4),
        fold_net_pnls=net_pnls, fold_win_rates=win_rates,
        average_net_pnl=round(sum(net_pnls) / len(net_pnls), 2),
        worst_fold_net_pnl=round(min(net_pnls), 2),
        any_fold_kill_switch_triggered=any(f.result.kill_switch_triggered for f in folds),
    )


class WalkForwardEngine:
    def __init__(self, candle_repo, risk_settings, news_repo=None) -> None:
        self.candle_repo = candle_repo
        self.backtest_engine = BacktestEngine(candle_repo, risk_settings, news_repo=news_repo)

    async def run(
        self, config: BacktestConfig, symbol_rules: SymbolRules,
        window_bars: int, step_bars: int, total_bars: int = 2000,
    ) -> WalkForwardResult:
        if window_bars <= config.warmup_bars:
            raise ValueError(
                f"window_bars ({window_bars}) must exceed config.warmup_bars ({config.warmup_bars}) - "
                "a fold needs bars left over after warmup to actually trade on"
            )
        if step_bars <= 0:
            raise ValueError("step_bars must be positive")

        df = await self.candle_repo.fetch_ohlcv(
            config.symbol, config.interval, limit=total_bars, closed_only=True,
        )
        if len(df) < window_bars:
            raise ValueError(
                f"not enough history for {config.symbol}/{config.interval} to run even one "
                f"walk-forward fold: {len(df)} candles, need at least {window_bars}"
            )

        news_history = await self.backtest_engine.fetch_news_history(config, df)

        folds: list[WalkForwardFold] = []
        start = 0
        fold_index = 0
        while start + window_bars <= len(df):
            window_df = df.iloc[start:start + window_bars].reset_index(drop=True)
            result = self.backtest_engine.run_over_dataframe(window_df, config, symbol_rules, news_history=news_history)
            folds.append(WalkForwardFold(
                fold_index=fold_index, window_start=window_df["open_time"].iloc[0],
                window_end=window_df["close_time"].iloc[-1], result=result,
            ))
            fold_index += 1
            start += step_bars

        if not folds:
            raise ValueError(
                f"{len(df)} candles and window_bars={window_bars} produced zero folds - "
                "this should be unreachable given the length check above"
            )

        aggregate = _fold_metrics(folds)
        aggregate.window_bars = window_bars
        aggregate.step_bars = step_bars
        return aggregate
