import pytest

from aegis.risk.rules import (
    check_liquidation_distance,
    compute_drawdown_pct,
    compute_drawdown_state,
    compute_net_r_multiple,
    estimate_liquidation_distance_pct,
    loss_streak_action,
)


def test_compute_drawdown_pct_matches_hand_computed_value():
    assert compute_drawdown_pct(90.0, 100.0) == pytest.approx(0.10)


def test_compute_drawdown_pct_zero_when_at_or_above_peak():
    assert compute_drawdown_pct(100.0, 100.0) == pytest.approx(0.0)
    assert compute_drawdown_pct(110.0, 100.0) == pytest.approx(0.0)  # new high, not negative drawdown


def test_compute_drawdown_pct_zero_peak_is_safe():
    assert compute_drawdown_pct(50.0, 0.0) == pytest.approx(0.0)


@pytest.mark.parametrize(
    "equity,peak,expected",
    [
        (100.0, 100.0, "NORMAL"),
        (96.0, 100.0, "NORMAL"),        # 4% dd < 5% caution threshold
        (94.0, 100.0, "CAUTION"),       # 6% dd >= 5% caution
        (91.0, 100.0, "REDUCED_RISK"),  # 9% dd >= 7.5% reduced_risk
        (85.0, 100.0, "HALTED"),        # 15% dd >= 10% halted
    ],
)
def test_compute_drawdown_state_thresholds(equity, peak, expected):
    state = compute_drawdown_state(equity, peak, caution_pct=0.05, reduced_risk_pct=0.075, halted_pct=0.10)
    assert state == expected


def test_compute_drawdown_state_boundary_is_inclusive():
    # exactly at the halted threshold should halt, not wait for "more than"
    assert compute_drawdown_state(90.0, 100.0, caution_pct=0.05, reduced_risk_pct=0.075, halted_pct=0.10) == "HALTED"


@pytest.mark.parametrize(
    "losses,expected",
    [(0, "NONE"), (2, "NONE"), (3, "COOLDOWN"), (4, "COOLDOWN"), (5, "REDUCE_RISK"), (7, "REDUCE_RISK"), (8, "HALT"), (12, "HALT")],
)
def test_loss_streak_action_thresholds(losses, expected):
    action = loss_streak_action(losses, cooldown_threshold=3, reduce_risk_threshold=5, halt_threshold=8)
    assert action == expected


def test_compute_net_r_multiple_matches_hand_computed_value():
    # entry=100, stop=98 (risk=2), tp=106 (reward=6), fees+slippage=0.13% of entry=0.13
    r = compute_net_r_multiple(100.0, 98.0, 106.0, fees_pct=0.0008, slippage_pct=0.0005)
    cost = 100.0 * 0.0013
    expected = (6.0 - cost) / (2.0 + cost)
    assert r == pytest.approx(expected)


def test_compute_net_r_multiple_none_when_stop_equals_entry():
    assert compute_net_r_multiple(100.0, 100.0, 110.0) is None


def test_compute_net_r_multiple_never_negative_reward():
    # take profit so close that costs eat the entire reward - reward floors at 0, not negative
    r = compute_net_r_multiple(100.0, 99.0, 100.05, fees_pct=0.01, slippage_pct=0.0)
    assert r >= 0.0


def test_estimate_liquidation_distance_pct_matches_hand_computed_value():
    # leverage=10 -> 1/10=0.10 minus maintenance margin 0.004 = 0.096
    assert estimate_liquidation_distance_pct(10, 0.004) == pytest.approx(0.096)


def test_estimate_liquidation_distance_pct_rejects_non_positive_leverage():
    with pytest.raises(ValueError):
        estimate_liquidation_distance_pct(0, 0.004)


def test_check_liquidation_distance_safe_for_a_tight_stop():
    result = check_liquidation_distance(
        entry_price=100.0, stop_price=99.0, leverage=5, maintenance_margin_rate=0.004, safety_margin=0.75,
    )
    # stop_distance_pct=0.01; liq_distance=1/5-0.004=0.196; 0.196*0.75=0.147 -> 0.01 <= 0.147 -> safe
    assert result.safe is True
    assert result.stop_distance_pct == pytest.approx(0.01)


def test_check_liquidation_distance_unsafe_for_a_wide_stop_at_high_leverage():
    result = check_liquidation_distance(
        entry_price=100.0, stop_price=90.0, leverage=20, maintenance_margin_rate=0.004, safety_margin=0.75,
    )
    # stop_distance_pct=0.10; liq_distance=1/20-0.004=0.046; 0.046*0.75=0.0345 -> 0.10 > 0.0345 -> unsafe
    assert result.safe is False
