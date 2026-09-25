"""Tests for aegis.backtest.walk_forward.WalkForwardEngine (Fase 17) - the
previously-missing blueprint validation step ("LIVE só é liberado depois
que a mesma estratégia... passou por... WALK-FORWARD aprovado"). Uses the
exact same fake in-memory candle repo pattern as test_backtest_engine.py.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest

from aegis.backtest.models import BacktestConfig
from aegis.backtest.walk_forward import WalkForwardEngine
from aegis.providers.binance.models import SymbolRules
from aegis.strategy.strategies import STRATEGY_BREAKOUT


def _rules() -> SymbolRules:
    return SymbolRules(symbol="BTCUSDT", status="TRADING", price_precision=2, quantity_precision=5,
                        tick_size=0.1, step_size=0.00001, min_notional=5.0)


def _candles_df(closes: np.ndarray, volume: np.ndarray,
                 start: datetime = datetime(2026, 1, 1, tzinfo=UTC)) -> pd.DataFrame:
    n = len(closes)
    open_time = pd.date_range(start, periods=n, freq="1h")
    highs = closes + 0.3
    lows = closes - 0.3
    opens = np.concatenate([[closes[0]], closes[:-1]])
    return pd.DataFrame({
        "open_time": open_time,
        "close_time": open_time + pd.Timedelta(hours=1) - pd.Timedelta(milliseconds=1),
        "open": opens, "high": highs, "low": lows, "close": closes,
        "volume": volume, "quote_volume": closes * volume,
        "trades": np.full(n, 50), "taker_buy_base_volume": volume * 0.5,
        "taker_buy_quote_volume": closes * volume * 0.5, "is_closed": True,
    })


def _random_walk(n: int, seed: int = 11) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    closes = 100.0 + np.cumsum(rng.normal(0, 0.4, n))
    volume = np.abs(np.full(n, 100.0) + rng.normal(0, 5, n))
    return closes, volume


class _FakeCandleRepo:
    def __init__(self, df: pd.DataFrame):
        self._df = df

    async def fetch_ohlcv(self, symbol, interval, limit=500, closed_only=True):
        return self._df.tail(limit).reset_index(drop=True)


class _Settings:
    risk_per_trade = 0.01
    max_daily_loss = 0.10
    max_drawdown = 0.50
    drawdown_caution_pct = 0.20
    drawdown_reduced_risk_pct = 0.30
    risk_reduction_factor_caution = 0.75
    risk_reduction_factor_reduced = 0.50
    risk_reduction_factor_loss_streak = 0.50
    loss_streak_cooldown_threshold = 3
    loss_streak_reduce_risk_threshold = 5
    loss_streak_halt_threshold = 20
    max_leverage = 5
    min_r_multiple = 0.1
    max_open_positions = 10
    max_correlated_exposure_pct = 0.90
    maintenance_margin_rate_estimate = 0.004
    liquidation_safety_margin = 0.75


# -- validation ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_raises_when_window_bars_not_greater_than_warmup_bars():
    df = _candles_df(*_random_walk(500))
    engine = WalkForwardEngine(_FakeCandleRepo(df), _Settings())
    config = BacktestConfig(symbol="BTCUSDT", interval="1h", warmup_bars=210)
    with pytest.raises(ValueError):
        await engine.run(config, _rules(), window_bars=200, step_bars=100)


@pytest.mark.asyncio
async def test_raises_when_step_bars_not_positive():
    df = _candles_df(*_random_walk(500))
    engine = WalkForwardEngine(_FakeCandleRepo(df), _Settings())
    config = BacktestConfig(symbol="BTCUSDT", interval="1h", warmup_bars=210)
    with pytest.raises(ValueError):
        await engine.run(config, _rules(), window_bars=260, step_bars=0)


@pytest.mark.asyncio
async def test_raises_when_not_enough_history_for_even_one_fold():
    df = _candles_df(*_random_walk(100))
    engine = WalkForwardEngine(_FakeCandleRepo(df), _Settings())
    config = BacktestConfig(symbol="BTCUSDT", interval="1h", warmup_bars=20)
    with pytest.raises(ValueError):
        await engine.run(config, _rules(), window_bars=260, step_bars=260)


# -- fold mechanics (real end-to-end, no mocking) -----------------------------

@pytest.mark.asyncio
async def test_produces_the_expected_number_of_non_overlapping_folds():
    df = _candles_df(*_random_walk(800))
    engine = WalkForwardEngine(_FakeCandleRepo(df), _Settings())
    config = BacktestConfig(symbol="BTCUSDT", interval="1h", strategy_ids=(STRATEGY_BREAKOUT,), warmup_bars=210)

    result = await engine.run(config, _rules(), window_bars=260, step_bars=260, total_bars=800)

    # floor((800 - 260) / 260) + 1 = 3
    assert len(result.folds) == 3
    assert result.window_bars == 260
    assert result.step_bars == 260


@pytest.mark.asyncio
async def test_fold_windows_are_sequential_and_non_overlapping_when_step_equals_window():
    df = _candles_df(*_random_walk(800))
    engine = WalkForwardEngine(_FakeCandleRepo(df), _Settings())
    config = BacktestConfig(symbol="BTCUSDT", interval="1h", strategy_ids=(STRATEGY_BREAKOUT,), warmup_bars=210)

    result = await engine.run(config, _rules(), window_bars=260, step_bars=260, total_bars=800)

    for i in range(len(result.folds) - 1):
        assert result.folds[i].window_end < result.folds[i + 1].window_start
    assert result.folds[0].fold_index == 0
    assert result.folds[-1].fold_index == len(result.folds) - 1


@pytest.mark.asyncio
async def test_overlapping_folds_when_step_bars_is_smaller_than_window_bars():
    df = _candles_df(*_random_walk(800))
    engine = WalkForwardEngine(_FakeCandleRepo(df), _Settings())
    config = BacktestConfig(symbol="BTCUSDT", interval="1h", strategy_ids=(STRATEGY_BREAKOUT,), warmup_bars=210)

    result = await engine.run(config, _rules(), window_bars=260, step_bars=130, total_bars=800)

    # floor((800 - 260) / 130) + 1 = 5, and consecutive windows must overlap
    assert len(result.folds) == 5
    assert result.folds[1].window_start < result.folds[0].window_end


@pytest.mark.asyncio
async def test_each_fold_starts_from_the_same_configured_initial_equity():
    # Independence check: a fold with a losing run must not drag down the
    # NEXT fold's starting point - each fold is its own isolated backtest.
    df = _candles_df(*_random_walk(800))
    engine = WalkForwardEngine(_FakeCandleRepo(df), _Settings())
    config = BacktestConfig(symbol="BTCUSDT", interval="1h", initial_equity=1000.0,
                             strategy_ids=(STRATEGY_BREAKOUT,), warmup_bars=210)

    result = await engine.run(config, _rules(), window_bars=260, step_bars=260, total_bars=800)

    for fold in result.folds:
        assert fold.result.config.initial_equity == pytest.approx(1000.0)


# -- aggregate metrics (mocked fold outcomes, deterministic) ------------------

@dataclass
class _FakeMetrics:
    net_pnl: float
    win_rate: float


@dataclass
class _FakeConfig:
    symbol: str = "BTCUSDT"
    interval: str = "1h"


@dataclass
class _FakeResult:
    metrics: _FakeMetrics
    config: _FakeConfig
    kill_switch_triggered: bool = False


@pytest.mark.asyncio
async def test_profitable_fold_pct_and_aggregate_stats(monkeypatch):
    df = _candles_df(*_random_walk(800))
    engine = WalkForwardEngine(_FakeCandleRepo(df), _Settings())
    config = BacktestConfig(symbol="BTCUSDT", interval="1h", strategy_ids=(STRATEGY_BREAKOUT,), warmup_bars=210)

    # Three folds: two profitable, one a loss, one kill-switch trip - fully
    # controlled outcomes so the aggregate math itself is what's verified,
    # not whatever a real strategy happens to do on random data.
    fake_results = [
        _FakeResult(metrics=_FakeMetrics(net_pnl=100.0, win_rate=0.6), config=_FakeConfig()),
        _FakeResult(metrics=_FakeMetrics(net_pnl=-50.0, win_rate=0.3), config=_FakeConfig(), kill_switch_triggered=True),
        _FakeResult(metrics=_FakeMetrics(net_pnl=30.0, win_rate=0.55), config=_FakeConfig()),
    ]
    calls = iter(fake_results)
    monkeypatch.setattr(engine.backtest_engine, "run_over_dataframe", lambda *a, **k: next(calls))

    result = await engine.run(config, _rules(), window_bars=260, step_bars=260, total_bars=800)

    assert len(result.folds) == 3
    assert result.fold_net_pnls == [100.0, -50.0, 30.0]
    assert result.fold_win_rates == [0.6, 0.3, 0.55]
    assert result.profitable_fold_pct == pytest.approx(2 / 3, abs=1e-3)
    assert result.average_net_pnl == pytest.approx((100.0 - 50.0 + 30.0) / 3, abs=1e-2)
    assert result.worst_fold_net_pnl == pytest.approx(-50.0)
    assert result.any_fold_kill_switch_triggered is True


@pytest.mark.asyncio
async def test_no_kill_switch_trips_reports_false(monkeypatch):
    df = _candles_df(*_random_walk(800))
    engine = WalkForwardEngine(_FakeCandleRepo(df), _Settings())
    config = BacktestConfig(symbol="BTCUSDT", interval="1h", strategy_ids=(STRATEGY_BREAKOUT,), warmup_bars=210)

    fake_results = [
        _FakeResult(metrics=_FakeMetrics(net_pnl=10.0, win_rate=0.5), config=_FakeConfig()),
        _FakeResult(metrics=_FakeMetrics(net_pnl=20.0, win_rate=0.5), config=_FakeConfig()),
        _FakeResult(metrics=_FakeMetrics(net_pnl=30.0, win_rate=0.5), config=_FakeConfig()),
    ]
    calls = iter(fake_results)
    monkeypatch.setattr(engine.backtest_engine, "run_over_dataframe", lambda *a, **k: next(calls))

    result = await engine.run(config, _rules(), window_bars=260, step_bars=260, total_bars=800)

    assert result.any_fold_kill_switch_triggered is False
    assert result.profitable_fold_pct == pytest.approx(1.0)
