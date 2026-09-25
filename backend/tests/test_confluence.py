from datetime import UTC, datetime

import pytest

from aegis.strategy.confluence import combine_signals
from aegis.strategy.models import StrategySignal

_AS_OF = datetime(2026, 9, 21, tzinfo=UTC)


def _signal(strategy_id, signal, strength, insufficient_data=False) -> StrategySignal:
    return StrategySignal(strategy_id, "BTCUSDT", "1h", _AS_OF, signal, strength, [], insufficient_data=insufficient_data)


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


# -- insufficient_data abstentions (Fase 17h regression) ----------------------
# Real production bug found live 2026-09-25, hours after activating a 4th
# strategy: combine_signals divided by the total weight of every signal it
# was handed, so a strategy that almost always has no real opinion
# (missing required data, not a genuine "conditions didn't align" NO_TRADE)
# was silently diluting every OTHER strategy's vote just by being present -
# net_score dropped ~25% on a real reproduction, enough to push
# previously-passing signals below decision_threshold and silence entries
# for hours. An insufficient_data=True signal must be excluded from the
# weighted average entirely, not just contribute a zero-strength vote.

def test_an_abstaining_signal_does_not_change_the_net_score():
    without_abstention = combine_signals([_signal("A", "LONG", 90), _signal("B", "LONG", 80)])
    with_abstention = combine_signals([
        _signal("A", "LONG", 90), _signal("B", "LONG", 80),
        _signal("C", "NO_TRADE", 0, insufficient_data=True),
    ])
    assert with_abstention.net_score == pytest.approx(without_abstention.net_score)


def test_an_abstaining_signal_can_still_flip_a_borderline_decision_if_wrongly_counted():
    # Concrete reproduction of the real bug: 2 strategies clear
    # decision_threshold=45 with 3 total "voters", but would NOT clear it
    # if a 4th abstaining strategy were wrongly counted in the denominator
    # (net_score would drop from 60 to 45, landing exactly on the
    # threshold rather than above it). This proves the fix, not just that
    # nothing changed.
    signals = [
        _signal("A", "LONG", 90), _signal("B", "LONG", 90), _signal("C", "NO_TRADE", 0),
        _signal("D", "NO_TRADE", 0, insufficient_data=True),
    ]
    result = combine_signals(signals, decision_threshold=45.0)
    assert result.net_score == pytest.approx(60.0)
    assert result.decision == "LONG"


def test_a_non_abstaining_no_trade_signal_still_counts_in_the_denominator():
    # A strategy that DID look at real data and genuinely found nothing
    # must still count as a real vote (it's real information, e.g. "the
    # squeeze conditions existed but didn't align") - only a missing-data
    # abstention is excluded.
    signals = [_signal("A", "LONG", 90), _signal("B", "LONG", 90), _signal("C", "NO_TRADE", 0)]
    result = combine_signals(signals, decision_threshold=45.0)
    assert result.net_score == pytest.approx(60.0)  # (90+90+0)/3, not (90+90)/2


def test_all_signals_abstaining_yields_no_trade_without_dividing_by_zero():
    signals = [_signal("A", "NO_TRADE", 0, insufficient_data=True), _signal("B", "NO_TRADE", 0, insufficient_data=True)]
    result = combine_signals(signals)
    assert result.decision == "NO_TRADE"
    assert result.net_score == pytest.approx(0.0)
