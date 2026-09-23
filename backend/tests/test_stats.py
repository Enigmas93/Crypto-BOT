import pandas as pd
import pytest

from aegis import stats as analytics


def test_latest_pct_change_matches_hand_computed_value():
    s = pd.Series([100.0, 110.0])
    assert analytics.latest_pct_change(s) == pytest.approx(10.0)


def test_latest_pct_change_none_with_fewer_than_two_points():
    assert analytics.latest_pct_change(pd.Series([100.0])) is None
    assert analytics.latest_pct_change(pd.Series([], dtype=float)) is None


def test_latest_pct_change_none_when_previous_is_zero():
    s = pd.Series([0.0, 5.0])
    assert analytics.latest_pct_change(s) is None


def test_pct_change_over_matches_hand_computed_value():
    # 12 monthly points, first=100, last=112 -> 12% year-over-year
    s = pd.Series([100.0] + [0.0] * 10 + [112.0])
    assert analytics.pct_change_over(s, periods=11) == pytest.approx(12.0)


def test_pct_change_over_none_without_enough_history():
    s = pd.Series([100.0, 105.0, 110.0])
    assert analytics.pct_change_over(s, periods=11) is None


def test_pct_change_over_one_matches_latest_pct_change():
    s = pd.Series([100.0, 110.0])
    assert analytics.pct_change_over(s, periods=1) == pytest.approx(analytics.latest_pct_change(s))


def test_latest_zscore_none_below_min_points():
    s = pd.Series([1.0, 2.0, 3.0])
    assert analytics.latest_zscore(s, min_points=5) is None


def test_latest_zscore_none_when_series_is_flat():
    s = pd.Series([5.0] * 10)
    assert analytics.latest_zscore(s) is None  # zero variance -> undefined, not a lie


def test_latest_zscore_of_an_outlier_is_large_and_positive():
    s = pd.Series([10.0] * 19 + [1000.0])
    z = analytics.latest_zscore(s)
    assert z > 3.0


def test_latest_zscore_matches_hand_computed_value():
    s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    # mean=3, population std = sqrt(2) ~= 1.4142; last value 5 -> z = 2/1.4142
    z = analytics.latest_zscore(s, min_points=5)
    assert z == pytest.approx((5 - 3) / (2 ** 0.5))


def test_acceleration_none_below_three_points():
    assert analytics.acceleration(pd.Series([1.0, 2.0])) is None


def test_acceleration_zero_for_constant_growth_rate():
    # 100 -> 110 (+10%) -> 121 (+10%): the rate of change didn't change
    s = pd.Series([100.0, 110.0, 121.0])
    assert analytics.acceleration(s) == pytest.approx(0.0, abs=1e-9)


def test_acceleration_positive_when_growth_rate_speeds_up():
    # 100 -> 110 (+10%) -> 143 (+30%): the growth rate itself increased
    s = pd.Series([100.0, 110.0, 143.0])
    result = analytics.acceleration(s)
    assert result > 0
    assert result == pytest.approx(20.0)
