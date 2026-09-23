"""Tests the in-memory Kill Switch simulation inside BacktestEngine.run()
(spec section 23) - see the module docstring in `aegis/backtest/engine.py`
for why this never touches the real, persisted KillSwitchRepository.
"""
from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest

from aegis.backtest.engine import BacktestEngine
from aegis.backtest.models import BacktestConfig
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


def _consolidation_then_breakout(n: int = 320, consolidation_len: int = 260, seed: int = 7):
    """Same generator as test_backtest_engine.py - a flat, choppy range
    followed by a sharp breakout with a genuine volume spike, chosen
    empirically to reliably fire STRATEGY_BREAKOUT (including some losing
    trades along the way, which is exactly what this test needs)."""
    rng = np.random.default_rng(seed)
    closes = np.empty(n)
    closes[:consolidation_len] = 100.0 + rng.normal(0, 0.5, consolidation_len)
    breakout_len = n - consolidation_len
    closes[consolidation_len:] = 100.0 + np.linspace(2, 20, breakout_len) + rng.normal(0, 0.3, breakout_len)

    volume = np.abs(np.full(n, 100.0) + rng.normal(0, 5, n))
    volume[consolidation_len:consolidation_len + 5] *= 4.0
    return closes, volume


class _Settings:
    risk_per_trade = 0.01
    max_daily_loss = 0.10
    max_drawdown = 0.99  # effectively disabled for the loss-streak test below
    drawdown_caution_pct = 0.20
    drawdown_reduced_risk_pct = 0.30
    risk_reduction_factor_caution = 0.75
    risk_reduction_factor_reduced = 0.50
    risk_reduction_factor_loss_streak = 0.50
    loss_streak_cooldown_threshold = 3
    loss_streak_reduce_risk_threshold = 5
    loss_streak_halt_threshold = 1  # trips the kill switch on the very first loss
    max_leverage = 5
    min_r_multiple = 0.1
    max_open_positions = 10
    max_correlated_exposure_pct = 0.90
    maintenance_margin_rate_estimate = 0.004
    liquidation_safety_margin = 0.75


class _NeverTripSettings(_Settings):
    max_drawdown = 0.99
    loss_streak_halt_threshold = 20  # never reached by a handful of trades over 320 bars


class _FakeCandleRepo:
    def __init__(self, df: pd.DataFrame):
        self._df = df

    async def fetch_ohlcv(self, symbol, interval, limit=500, closed_only=True):
        return self._df.tail(limit).reset_index(drop=True)


@pytest.mark.asyncio
async def test_kill_switch_trips_on_first_loss_and_blocks_every_later_entry():
    closes, volume = _consolidation_then_breakout()
    df = _candles_df(closes, volume)
    engine = BacktestEngine(_FakeCandleRepo(df), _Settings())
    config = BacktestConfig(symbol="BTCUSDT", interval="1h", initial_equity=1000.0,
                             strategy_ids=(STRATEGY_BREAKOUT,), warmup_bars=210)

    result = await engine.run(config, _rules())

    assert len(result.trades) > 0, "test needs at least one trade to be meaningful"
    losses = [t for t in result.trades if t.net_pnl < 0]
    assert losses, "test needs at least one losing trade to exercise the loss-streak trigger"

    assert result.kill_switch_triggered is True
    assert result.kill_switch_reasons == ["LOSS_STREAK_HALT_THRESHOLD"]
    assert result.kill_switch_tripped_at is not None

    # the trip fires the moment the first loss closes - every trade's exit
    # (and therefore its entry too, since entry always precedes exit) must
    # sit at or before that moment, and nothing may have entered after it
    assert all(t.entry_time < result.kill_switch_tripped_at for t in result.trades)
    assert result.trades[-1].exit_time == result.kill_switch_tripped_at
    assert result.trades[-1].net_pnl < 0


@pytest.mark.asyncio
async def test_kill_switch_does_not_trigger_when_thresholds_are_never_breached():
    closes, volume = _consolidation_then_breakout()
    df = _candles_df(closes, volume)
    engine = BacktestEngine(_FakeCandleRepo(df), _NeverTripSettings())
    config = BacktestConfig(symbol="BTCUSDT", interval="1h", initial_equity=1000.0,
                             strategy_ids=(STRATEGY_BREAKOUT,), warmup_bars=210)

    result = await engine.run(config, _rules())

    assert result.kill_switch_triggered is False
    assert result.kill_switch_reasons == []
    assert result.kill_switch_tripped_at is None
