import pandas as pd
import pytest

from aegis.technical.market_structure import analyze_market_structure, find_swing_points


def _df_from_prices(prices: list[float]) -> pd.DataFrame:
    # high == low == close simplifies hand-verifying which bars are swings:
    # an isolated local max registers only as a swing high, never also a low.
    return pd.DataFrame({
        "open_time": pd.date_range("2026-01-01", periods=len(prices), freq="1h", tz="UTC"),
        "high": prices,
        "low": prices,
        "close": prices,
    })


def test_find_swing_points_detects_one_isolated_peak():
    df = _df_from_prices([1, 1, 1, 10, 1, 1, 1])
    swings = find_swing_points(df, left=2, right=2)
    assert len(swings) == 1
    assert swings[0].kind == "high"
    assert swings[0].price == 10
    assert swings[0].index == 3


def test_find_swing_points_ignores_a_flat_series():
    df = _df_from_prices([5] * 10)
    assert find_swing_points(df, left=2, right=2) == []


# trough(90) -> peak(120) -> higher trough(95) -> higher peak(140) -> breakout candle(150)
_UPTREND_PRICES = [100, 95, 90, 95, 100, 110, 120, 115, 108, 100, 95, 100, 110, 125, 140, 135, 128, 150]


def test_swing_sequence_matches_hand_designed_uptrend():
    df = _df_from_prices(_UPTREND_PRICES)
    swings = find_swing_points(df, left=2, right=2)
    highs = [(s.index, s.price) for s in swings if s.kind == "high"]
    lows = [(s.index, s.price) for s in swings if s.kind == "low"]
    assert highs == [(6, 120), (14, 140)]
    assert lows == [(2, 90), (10, 95)]


def test_market_structure_classifies_uptrend_and_detects_breakout():
    df = _df_from_prices(_UPTREND_PRICES)
    result = analyze_market_structure(df, left=2, right=2)
    assert result.trend == "UPTREND"
    assert result.last_swing_high == 140
    assert result.last_swing_low == 95
    assert result.breakout is True  # last close (150) > last confirmed swing high (140)
    assert result.breakdown is False


def test_market_structure_classifies_downtrend():
    downtrend = list(reversed(_UPTREND_PRICES))
    df = _df_from_prices(downtrend)
    result = analyze_market_structure(df, left=2, right=2)
    assert result.trend == "DOWNTREND"


def test_market_structure_unknown_with_too_little_data():
    df = _df_from_prices([100, 101, 102])
    result = analyze_market_structure(df, left=2, right=2)
    assert result.trend == "UNKNOWN"
    assert result.last_swing_high is None
    assert result.breakout is False


def test_breakdown_detected_when_close_falls_below_last_swing_low():
    prices = _UPTREND_PRICES[:-1] + [50]  # replace the breakout candle with a crash
    df = _df_from_prices(prices)
    result = analyze_market_structure(df, left=2, right=2)
    assert result.breakdown is True
    assert result.breakout is False
