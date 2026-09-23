from aegis.risk.kill_switch import (
    KILL_SWITCH_LOSS_STREAK,
    KILL_SWITCH_MAX_DRAWDOWN,
    evaluate_kill_switch_triggers,
)


def test_no_triggers_under_normal_conditions():
    reasons = evaluate_kill_switch_triggers(
        equity=1000.0, peak_equity=1000.0, consecutive_losses=0,
        max_drawdown=0.10, loss_streak_halt_threshold=8,
    )
    assert reasons == []


def test_max_drawdown_triggers_at_the_threshold_inclusive():
    # peak=1000, equity=900 -> drawdown exactly 10%
    reasons = evaluate_kill_switch_triggers(
        equity=900.0, peak_equity=1000.0, consecutive_losses=0,
        max_drawdown=0.10, loss_streak_halt_threshold=8,
    )
    assert reasons == [KILL_SWITCH_MAX_DRAWDOWN]


def test_max_drawdown_does_not_trigger_just_below_the_threshold():
    reasons = evaluate_kill_switch_triggers(
        equity=901.0, peak_equity=1000.0, consecutive_losses=0,
        max_drawdown=0.10, loss_streak_halt_threshold=8,
    )
    assert reasons == []


def test_loss_streak_triggers_at_the_threshold_inclusive():
    reasons = evaluate_kill_switch_triggers(
        equity=1000.0, peak_equity=1000.0, consecutive_losses=8,
        max_drawdown=0.10, loss_streak_halt_threshold=8,
    )
    assert reasons == [KILL_SWITCH_LOSS_STREAK]


def test_loss_streak_does_not_trigger_just_below_the_threshold():
    reasons = evaluate_kill_switch_triggers(
        equity=1000.0, peak_equity=1000.0, consecutive_losses=7,
        max_drawdown=0.10, loss_streak_halt_threshold=8,
    )
    assert reasons == []


def test_both_triggers_accumulate_rather_than_short_circuit():
    reasons = evaluate_kill_switch_triggers(
        equity=850.0, peak_equity=1000.0, consecutive_losses=10,
        max_drawdown=0.10, loss_streak_halt_threshold=8,
    )
    assert reasons == [KILL_SWITCH_MAX_DRAWDOWN, KILL_SWITCH_LOSS_STREAK]


def test_zero_peak_equity_never_triggers_drawdown_via_division_by_zero():
    reasons = evaluate_kill_switch_triggers(
        equity=0.0, peak_equity=0.0, consecutive_losses=0,
        max_drawdown=0.10, loss_streak_halt_threshold=8,
    )
    assert reasons == []
