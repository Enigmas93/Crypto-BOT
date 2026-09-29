from datetime import UTC, datetime, timedelta

from aegis.health.watchdog import AlertTracker, HealthInputs, evaluate_health, last_closed_bar_close

NOW = datetime(2026, 9, 29, 11, 20, tzinfo=UTC)
BAR_10 = datetime(2026, 9, 29, 10, 59, 59, 999000, tzinfo=UTC)   # 10:00 bar close
BAR_09 = datetime(2026, 9, 29, 9, 59, 59, 999000, tzinfo=UTC)


def _inputs(**over):
    base = dict(
        now=NOW, candle_latest_close={"BTCUSDT": NOW - timedelta(minutes=1)},
        cursors={("paper", "BTCUSDT", "1h"): BAR_10, ("shadow_bingx_demo", "BTCUSDT", "1h"): BAR_10,
                 ("momentum_bingx_demo", "HBARUSDT", "1h"): BAR_10},
        expected_fixed=[("paper", "BTCUSDT", "1h"), ("shadow_bingx_demo", "BTCUSDT", "1h")],
        momentum_accounts=["momentum_bingx_demo"],
    )
    base.update(over)
    return HealthInputs(**base)


def test_last_closed_bar():
    assert last_closed_bar_close(NOW, "1h") == BAR_10
    assert last_closed_bar_close(datetime(2026, 9, 29, 11, 0, tzinfo=UTC), "1h") == BAR_10


def test_healthy_system_has_no_issues():
    assert evaluate_health(_inputs()) == []


def test_engine_that_missed_the_last_bar_is_flagged():
    issues = evaluate_health(_inputs(cursors={("paper", "BTCUSDT", "1h"): BAR_09,
                                              ("shadow_bingx_demo", "BTCUSDT", "1h"): BAR_10,
                                              ("momentum_bingx_demo", "HBARUSDT", "1h"): BAR_10}))
    assert [(i.key, i.severity) for i in issues] == [("engine:paper", "CRITICAL")]
    assert "BTCUSDT" in issues[0].detail


def test_grace_window_right_after_the_hour():
    # 11:05: the 10:00 bar closed 5 min ago - engines may still be evaluating it
    early = datetime(2026, 9, 29, 11, 5, tzinfo=UTC)
    cursors = {("paper", "BTCUSDT", "1h"): BAR_09, ("shadow_bingx_demo", "BTCUSDT", "1h"): BAR_09,
               ("momentum_bingx_demo", "HBARUSDT", "1h"): BAR_09}
    assert evaluate_health(_inputs(now=early, candle_latest_close={"BTCUSDT": early}, cursors=cursors)) == []


def test_open_position_pauses_the_cursor_without_alarm():
    inp = _inputs(cursors={("paper", "BTCUSDT", "1h"): BAR_09, ("shadow_bingx_demo", "BTCUSDT", "1h"): BAR_10,
                           ("momentum_bingx_demo", "HBARUSDT", "1h"): BAR_10},
                  open_positions={("paper", "BTCUSDT")})
    assert evaluate_health(inp) == []


def test_stale_data_reported_once_as_root_cause():
    inp = _inputs(candle_latest_close={"BTCUSDT": NOW - timedelta(minutes=55)},
                  cursors={("paper", "BTCUSDT", "1h"): BAR_09, ("shadow_bingx_demo", "BTCUSDT", "1h"): BAR_10,
                           ("momentum_bingx_demo", "HBARUSDT", "1h"): BAR_10})
    keys = [i.key for i in evaluate_health(inp)]
    assert keys == ["data:binance_candles"]  # paper's lag is a consequence, not a second alarm


def test_momentum_engine_with_no_recent_evaluation_is_flagged():
    inp = _inputs(cursors={("paper", "BTCUSDT", "1h"): BAR_10, ("shadow_bingx_demo", "BTCUSDT", "1h"): BAR_10,
                           ("momentum_bingx_demo", "HBARUSDT", "1h"): BAR_09})
    assert [i.key for i in evaluate_health(inp)] == ["engine:momentum_bingx_demo"]


def test_kill_switched_account_is_a_warning():
    issues = evaluate_health(_inputs(kill_switched=["momentum_bingx_demo"]))
    assert [(i.key, i.severity) for i in issues] == [("killswitch:momentum_bingx_demo", "WARNING")]


def test_alert_tracker_debounces_reminds_and_recovers():
    tracker = AlertTracker(confirm_checks=2, remind_after=timedelta(hours=3))
    issue = evaluate_health(_inputs(cursors={}))  # every engine lagging
    t0 = NOW
    alert, rec = tracker.update(issue, t0)
    assert alert == [] and rec == []                    # first sighting: could be a blip
    alert, _ = tracker.update(issue, t0 + timedelta(minutes=1))
    assert {i.key for i in alert} == {"engine:paper", "engine:shadow_bingx_demo", "engine:momentum_bingx_demo"}
    alert, _ = tracker.update(issue, t0 + timedelta(minutes=30))
    assert alert == []                                  # no spam while it persists
    alert, _ = tracker.update(issue, t0 + timedelta(hours=3, minutes=1))
    assert len(alert) == 3                              # reminder
    alert, rec = tracker.update([], t0 + timedelta(hours=3, minutes=2))
    assert alert == [] and len(rec) == 3                # recovered


def test_alert_tracker_ignores_one_off_blips_and_warnings():
    tracker = AlertTracker(confirm_checks=2)
    blip = evaluate_health(_inputs(cursors={}))
    tracker.update(blip, NOW)
    alert, rec = tracker.update([], NOW + timedelta(minutes=1))
    assert alert == [] and rec == []                    # never alerted, so no "recovered" either
    warn = evaluate_health(_inputs(kill_switched=["paper"]))
    assert tracker.update(warn, NOW)[0] == [] and tracker.update(warn, NOW + timedelta(minutes=1))[0] == []
