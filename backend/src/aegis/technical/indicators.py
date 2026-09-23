"""Pure indicator math (spec section 25).

Every function takes/returns pandas Series or DataFrames and is a pure
function of its input - no I/O, no state. `TechnicalAnalysisService` is the
only caller; that split keeps the math independently testable against
hand-computed values without touching the database.

All indicators use `min_periods` so a value is `NaN` (never a misleading
number) until enough history exists to compute it honestly - spec's
"nunca inventar dado" principle applies to indicators too.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period, min_periods=period).mean()


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    ranges = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    )
    return ranges.max(axis=1)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    tr = true_range(df)
    return tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    result = 100 - (100 / (1 + rs))
    # a genuinely flat average loss of zero means "no losses" -> RSI = 100
    result = result.where(avg_loss != 0, 100.0)
    return result.where(avg_gain.notna())


def adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    up_move = df["high"].diff()
    down_move = -df["low"].diff()
    plus_dm = ((up_move > down_move) & (up_move > 0)) * up_move
    minus_dm = ((down_move > up_move) & (down_move > 0)) * down_move

    tr_ema = true_range(df).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    plus_di = 100 * (plus_dm.ewm(alpha=1 / period, adjust=False, min_periods=period).mean() / tr_ema)
    minus_di = 100 * (minus_dm.ewm(alpha=1 / period, adjust=False, min_periods=period).mean() / tr_ema)

    di_sum = (plus_di + minus_di).replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / di_sum
    return dx.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def bollinger_bands(
    series: pd.Series, period: int = 20, num_std: float = 2.0
) -> tuple[pd.Series, pd.Series, pd.Series]:
    mid = sma(series, period)
    std = series.rolling(period, min_periods=period).std(ddof=0)
    return mid, mid + num_std * std, mid - num_std * std


def macd(
    series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[pd.Series, pd.Series, pd.Series]:
    macd_line = ema(series, fast) - ema(series, slow)
    signal_line = macd_line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return macd_line, signal_line, macd_line - signal_line


def roc(series: pd.Series, period: int = 12) -> pd.Series:
    shifted = series.shift(period)
    return (series / shifted - 1.0) * 100


def momentum(series: pd.Series, period: int = 10) -> pd.Series:
    return series - series.shift(period)


def daily_anchored_vwap(df: pd.DataFrame) -> pd.Series:
    """VWAP re-anchored at the start of each UTC day - the conventional
    definition, not a rolling window (spec section 25)."""
    typical_price = (df["high"] + df["low"] + df["close"]) / 3
    pv = typical_price * df["volume"]
    day = df["open_time"].dt.floor("D")
    cum_pv = pv.groupby(day).cumsum()
    cum_vol = df["volume"].groupby(day).cumsum()
    return cum_pv / cum_vol.replace(0, np.nan)


def volume_zscore(volume: pd.Series, lookback: int = 20) -> pd.Series:
    mean = volume.rolling(lookback, min_periods=lookback).mean()
    std = volume.rolling(lookback, min_periods=lookback).std(ddof=0)
    return (volume - mean) / std.replace(0, np.nan)


def percentile_rank(series: pd.Series, lookback: int = 100) -> pd.Series:
    """Where the latest value sits (0-1) within its own trailing window -
    used for the ATR volatility percentile (spec section 25)."""

    def _rank_last(window: np.ndarray) -> float:
        last = window[-1]
        return float((window <= last).sum()) / len(window)

    return series.rolling(lookback, min_periods=lookback).apply(_rank_last, raw=True)


def pct_distance(price: pd.Series, reference: pd.Series) -> pd.Series:
    return (price - reference) / reference.replace(0, np.nan) * 100
