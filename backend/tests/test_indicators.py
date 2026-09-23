import numpy as np
import pandas as pd
import pytest

from aegis.technical import indicators as ta


def test_sma_matches_hand_computed_rolling_mean():
    s = pd.Series([1, 2, 3, 4, 5, 6], dtype=float)
    result = ta.sma(s, period=3)
    assert result.isna().sum() == 2
    assert result.iloc[2:].tolist() == pytest.approx([2.0, 3.0, 4.0, 5.0])


def test_ema_seed_and_recursion_span_3():
    # span=3 -> alpha=0.5; adjust=False seeds y0=x0 then recurses.
    s = pd.Series([1, 2, 3, 4, 5, 6], dtype=float)
    result = ta.ema(s, period=3)
    assert result.isna().sum() == 2  # min_periods masks the first 2
    expected = [2.25, 3.125, 4.0625, 5.03125]
    assert result.iloc[2:].tolist() == pytest.approx(expected)


def test_rsi_is_100_for_a_strictly_increasing_series():
    s = pd.Series(np.arange(1, 30, dtype=float))  # always +1, never a loss
    result = ta.rsi(s, period=14)
    assert result.iloc[-1] == pytest.approx(100.0)


def test_rsi_is_0_for_a_strictly_decreasing_series():
    s = pd.Series(np.arange(30, 1, -1, dtype=float))  # always -1, never a gain
    result = ta.rsi(s, period=14)
    assert result.iloc[-1] == pytest.approx(0.0)


def test_rsi_is_nan_before_period_elapses():
    s = pd.Series(np.arange(1, 10, dtype=float))
    result = ta.rsi(s, period=14)
    assert result.isna().all()


def _flat_ohlc_df(n: int = 20) -> pd.DataFrame:
    return pd.DataFrame({
        "open_time": pd.date_range("2026-01-01", periods=n, freq="1min", tz="UTC"),
        "high": [101.0] * n,
        "low": [99.0] * n,
        "close": [100.0] * n,
        "volume": [10.0] * n,
    })


def test_atr_converges_to_constant_true_range():
    df = _flat_ohlc_df(20)
    result = ta.atr(df, period=14)
    assert result.iloc[-1] == pytest.approx(2.0)  # high-low=2, no gaps vs prev close


def test_bollinger_bands_collapse_to_sma_when_flat():
    s = pd.Series([100.0] * 25)
    mid, upper, lower = ta.bollinger_bands(s, period=20, num_std=2.0)
    assert mid.iloc[-1] == pytest.approx(100.0)
    assert upper.iloc[-1] == pytest.approx(100.0)
    assert lower.iloc[-1] == pytest.approx(100.0)


def test_macd_is_positive_for_a_sustained_uptrend():
    s = pd.Series(np.linspace(100, 200, 60))
    macd_line, signal_line, hist = ta.macd(s)
    assert macd_line.iloc[-1] > 0
    assert not pd.isna(signal_line.iloc[-1])


def test_roc_matches_hand_computed_percentage_change():
    s = pd.Series([100.0, 105.0, 110.0, 121.0])
    result = ta.roc(s, period=1)
    assert result.iloc[1] == pytest.approx(5.0)
    assert result.iloc[3] == pytest.approx(10.0)


def test_momentum_matches_hand_computed_difference():
    s = pd.Series([10.0, 12.0, 15.0, 20.0])
    result = ta.momentum(s, period=2)
    assert result.iloc[2] == pytest.approx(5.0)
    assert result.iloc[3] == pytest.approx(8.0)


def test_daily_anchored_vwap_resets_each_utc_day():
    df = pd.DataFrame({
        "open_time": pd.to_datetime([
            "2026-01-01T00:00:00Z", "2026-01-01T00:01:00Z", "2026-01-02T00:00:00Z",
        ]),
        "high": [102.0, 102.0, 50.0],
        "low": [98.0, 98.0, 50.0],
        "close": [100.0, 100.0, 50.0],
        "volume": [10.0, 30.0, 5.0],
    })
    result = ta.daily_anchored_vwap(df)
    # day 1: typical price is 100.0 throughout -> vwap stays 100 regardless of volume mix
    assert result.iloc[0] == pytest.approx(100.0)
    assert result.iloc[1] == pytest.approx(100.0)
    # day 2 resets - single bar, typical price 50
    assert result.iloc[2] == pytest.approx(50.0)


def test_volume_zscore_is_nan_when_flat_then_spikes_on_a_jump():
    volume = pd.Series([10.0] * 20 + [1000.0])
    result = ta.volume_zscore(volume, lookback=20)
    assert pd.isna(result.iloc[19])  # zero variance window -> undefined, not a lie
    assert result.iloc[20] > 3.0  # a 100x spike should read as a strong outlier


def test_percentile_rank_of_max_value_is_one():
    s = pd.Series(np.arange(1, 11, dtype=float))  # 1..10, strictly increasing
    result = ta.percentile_rank(s, lookback=10)
    assert result.iloc[-1] == pytest.approx(1.0)


def test_pct_distance_matches_hand_computed_percentage():
    price = pd.Series([110.0, 90.0])
    reference = pd.Series([100.0, 100.0])
    result = ta.pct_distance(price, reference)
    assert result.tolist() == pytest.approx([10.0, -10.0])
