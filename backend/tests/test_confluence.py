from datetime import UTC, datetime

import pytest

from aegis.strategy.confluence import combine_signals
from aegis.strategy.models import StrategySignal

_AS_OF = datetime(2026, 9, 21, tzinfo=UTC)


def _signal(strategy_id, signal, strength) -> StrategySignal:
    return StrategySignal(strategy_id, "BTCUSDT", "1h", _AS_OF, signal, strength, [])


def test_all_strategies_agree_long():
    signals = [_signal("A", "LONG", 80), _signal("B", "LONG", 70), _signal("C", "LONG", 90)]
    result = combine_signals(signals, decision_threshold=30.0)
    assert result.decision == "LONG"
    assert result.net_score > 0
    assert result.confluence_score == pytest.approx(result.net_score)


def test_all_strategies_agree_short():
    signals = [_signal("A", "SHORT", 80), _signal("B", "SHORT", 70)]
    result = combine_signals(signals, decision_threshold=30.0)
    assert result.decision == "SHORT"
    assert result.net_score < 0


def test_strategies_split_evenly_yields_no_trade():
    signals = [_signal("A", "LONG", 80), _signal("B", "SHORT", 80)]
    result = combine_signals(signals, decision_threshold=30.0)
    assert result.decision == "NO_TRADE"
    assert result.net_score == pytest.approx(0.0)


def test_weak_signals_below_threshold_yield_no_trade():
    signals = [_signal("A", "LONG", 20), _signal("B", "LONG", 15)]
    result = combine_signals(signals, decision_threshold=30.0)
    assert result.decision == "NO_TRADE"


def test_all_no_trade_yields_no_trade():
    signals = [_signal("A", "NO_TRADE", 0), _signal("B", "NO_TRADE", 0)]
    result = combine_signals(signals)
    assert result.decision == "NO_TRADE"
    assert result.confluence_score == pytest.approx(0.0)


def test_custom_weights_change_the_outcome():
    signals = [_signal("A", "LONG", 100), _signal("B", "SHORT", 100)]
    # equal weights -> perfectly cancels out -> NO_TRADE
    even = combine_signals(signals, weights={"A": 1.0, "B": 1.0}, decision_threshold=10.0)
    assert even.decision == "NO_TRADE"
    # A weighted much higher -> its LONG view should dominate
    weighted = combine_signals(signals, weights={"A": 5.0, "B": 1.0}, decision_threshold=10.0)
    assert weighted.decision == "LONG"


def test_single_strategy_signal_still_works():
    result = combine_signals([_signal("A", "LONG", 50)], decision_threshold=30.0)
    assert result.decision == "LONG"


def test_empty_signal_list_raises():
    with pytest.raises(ValueError):
        combine_signals([])


def test_result_carries_symbol_interval_as_of_from_the_signals():
    signals = [_signal("A", "LONG", 80)]
    result = combine_signals(signals)
    assert result.symbol == "BTCUSDT"
    assert result.interval == "1h"
    assert result.as_of == _AS_OF


def test_result_includes_every_input_signal_for_explainability():
    signals = [_signal("A", "LONG", 80), _signal("B", "SHORT", 20)]
    result = combine_signals(signals)
    assert result.strategy_signals == signals
