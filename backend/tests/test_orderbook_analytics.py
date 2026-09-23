import pytest

from aegis.orderbook import analytics


def test_microprice_matches_hand_computed_value():
    # bid=100 (qty 10), ask=101 (qty 5) -> heavier bid size pulls price toward ask
    result = analytics.microprice(100.0, 10.0, 101.0, 5.0)
    assert result == pytest.approx((100.0 * 5.0 + 101.0 * 10.0) / 15.0)
    assert result > 100.5  # closer to the ask than the naive mid, as expected


def test_microprice_equals_mid_when_sizes_are_equal():
    result = analytics.microprice(100.0, 10.0, 102.0, 10.0)
    assert result == pytest.approx(101.0)


def test_microprice_none_when_both_sizes_are_zero():
    assert analytics.microprice(100.0, 0.0, 101.0, 0.0) is None


def test_top_of_book_imbalance_matches_hand_computed_value():
    assert analytics.top_of_book_imbalance(30.0, 10.0) == pytest.approx(0.5)
    assert analytics.top_of_book_imbalance(10.0, 30.0) == pytest.approx(-0.5)
    assert analytics.top_of_book_imbalance(10.0, 10.0) == pytest.approx(0.0)


def test_top_of_book_imbalance_none_when_book_is_empty():
    assert analytics.top_of_book_imbalance(0.0, 0.0) is None


def test_depth_notional_matches_hand_computed_sum():
    levels = [(100.0, 2.0), (99.0, 3.0)]
    assert analytics.depth_notional(levels) == pytest.approx(100.0 * 2.0 + 99.0 * 3.0)


def test_depth_imbalance_matches_hand_computed_value():
    assert analytics.depth_imbalance(300.0, 100.0) == pytest.approx(0.5)
    assert analytics.depth_imbalance(0.0, 0.0) is None


def test_book_pressure_favors_top_of_book_over_deep_levels():
    # same total qty imbalance, but concentrated at the top in one case
    top_heavy_bids = [(100.0, 100.0), (99.0, 1.0), (98.0, 1.0)]
    top_heavy_asks = [(101.0, 1.0), (102.0, 1.0), (103.0, 1.0)]
    deep_heavy_bids = [(100.0, 1.0), (99.0, 1.0), (98.0, 100.0)]
    deep_heavy_asks = [(101.0, 1.0), (102.0, 1.0), (103.0, 1.0)]

    top_pressure = analytics.book_pressure(top_heavy_bids, top_heavy_asks)
    deep_pressure = analytics.book_pressure(deep_heavy_bids, deep_heavy_asks)

    assert top_pressure > deep_pressure > 0  # both bullish (more bid qty), top-heavy more so


def test_book_pressure_none_when_no_levels():
    assert analytics.book_pressure([], []) is None


def test_book_pressure_zero_when_perfectly_balanced():
    bids = [(100.0, 5.0), (99.0, 5.0)]
    asks = [(101.0, 5.0), (102.0, 5.0)]
    assert analytics.book_pressure(bids, asks) == pytest.approx(0.0)
