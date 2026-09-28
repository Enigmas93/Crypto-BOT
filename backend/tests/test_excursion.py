from aegis.db.excursion_repository import excursion_r


def test_long_excursion_in_r_uses_initial_stop_distance():
    # entry 100, stop 98 (1R = 2): best mark 103 -> +1.5R, worst 99 -> 0.5R against
    assert excursion_r("LONG", 100.0, 98.0, 103.0, 99.0, exit_price=98.0) == (1.5, 1.0)


def test_short_excursion_mirrors_long():
    assert excursion_r("SHORT", 100.0, 102.0, 97.0, 101.0, exit_price=101.0) == (1.5, 0.5)


def test_exit_price_counts_when_no_mark_was_recorded():
    assert excursion_r("LONG", 100.0, 98.0, None, None, exit_price=106.0) == (3.0, 0.0)


def test_zero_risk_returns_none():
    assert excursion_r("LONG", 100.0, 100.0, 101.0, 99.0, exit_price=100.0) == (None, None)
