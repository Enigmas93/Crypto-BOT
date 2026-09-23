import pytest

from aegis.providers.binance.models import SymbolRules
from aegis.risk.sizing import calculate_position_size, round_down_to_step


def _rules(step_size=0.001, min_notional=5.0) -> SymbolRules:
    return SymbolRules(symbol="BTCUSDT", status="TRADING", price_precision=2, quantity_precision=3,
                        tick_size=0.1, step_size=step_size, min_notional=min_notional)


def test_round_down_to_step_matches_hand_computed_value():
    assert round_down_to_step(1.2347, 0.001) == pytest.approx(1.234)


def test_round_down_to_step_avoids_float_drift():
    # the classic float trap: 0.1 isn't exactly representable in binary
    assert round_down_to_step(0.30000000000000004, 0.1) == pytest.approx(0.3)


def test_round_down_to_step_zero_step_returns_value_unchanged():
    assert round_down_to_step(1.23456, 0.0) == pytest.approx(1.23456)


def test_calculate_position_size_matches_hand_computed_value():
    # equity=1000, risk=0.5% -> risk_amount=5; entry=50000, stop=49000 -> stop_distance=1000
    # raw_qty = 5/1000 = 0.005 -> rounds down to step 0.001 -> 0.005 (already on-step)
    result = calculate_position_size(1000.0, 0.005, 50000.0, 49000.0, _rules())
    assert result.status == "OK"
    assert result.risk_amount == pytest.approx(5.0)
    assert result.stop_distance == pytest.approx(1000.0)
    assert result.quantity == pytest.approx(0.005)
    assert result.notional == pytest.approx(250.0)


def test_calculate_position_size_rounds_down_never_up():
    # raw_qty = 5/1000 = 0.005, but step=0.002 -> floor(0.005/0.002)=2 -> 0.004, not 0.006
    result = calculate_position_size(1000.0, 0.005, 50000.0, 49000.0, _rules(step_size=0.002))
    assert result.quantity == pytest.approx(0.004)


def test_calculate_position_size_no_stop_distance():
    result = calculate_position_size(1000.0, 0.005, 50000.0, 50000.0, _rules())
    assert result.status == "NO_STOP_DISTANCE"
    assert result.quantity == 0.0


def test_calculate_position_size_zero_quantity_when_step_rounds_to_nothing():
    # raw_qty is tiny (huge stop distance) and step is coarse -> rounds down to 0
    result = calculate_position_size(1000.0, 0.005, 50000.0, 10000.0, _rules(step_size=1.0))
    assert result.status == "ZERO_QUANTITY"


def test_calculate_position_size_below_min_notional():
    # risk_amount=0.09, stop_distance=1000 -> raw_qty=0.00009, fine step keeps
    # it nonzero (not ZERO_QUANTITY) but notional = 0.00009*50000 = 4.5 < 5.0
    result = calculate_position_size(
        1000.0, 0.00009, 50000.0, 49000.0, _rules(step_size=0.00001, min_notional=5.0),
    )
    assert result.status == "BELOW_MIN_NOTIONAL"
    assert result.quantity > 0


def test_calculate_position_size_higher_risk_per_trade_yields_larger_size():
    small = calculate_position_size(1000.0, 0.0025, 50000.0, 49000.0, _rules())
    large = calculate_position_size(1000.0, 0.01, 50000.0, 49000.0, _rules())
    assert large.quantity > small.quantity
