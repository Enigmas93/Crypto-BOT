from datetime import UTC, datetime

import pytest

from aegis.news.conflict import AssetNewsStatus
from aegis.strategy.strategies import (
    STRATEGY_BREAKOUT,
    STRATEGY_EVENT_REACTION,
    STRATEGY_MEAN_REVERSION,
    STRATEGY_TREND_PULLBACK,
    evaluate_all,
    evaluate_breakout,
    evaluate_event_reaction,
    evaluate_mean_reversion,
    evaluate_trend_pullback,
)
from aegis.technical.service import TechnicalSnapshot

_AS_OF = datetime(2026, 9, 21, tzinfo=UTC)


def _snapshot(**overrides) -> TechnicalSnapshot:
    base = dict(symbol="BTCUSDT", interval="1h", as_of=_AS_OF, data_points=300, quality="OK")
    base.update(overrides)
    return TechnicalSnapshot(**base)


# -- trend pullback -----------------------------------------------------------

def test_trend_pullback_long_on_full_bullish_confluence():
    snapshot = _snapshot(close=110.0, ema_50=105.0, ema_200=100.0, adx_14=25.0, rsi_14=50.0,
                          market_structure_trend="UPTREND")
    signal = evaluate_trend_pullback(snapshot)
    assert signal.signal == "LONG"
    assert signal.strength > 0
    assert signal.strategy_id == STRATEGY_TREND_PULLBACK


def test_trend_pullback_short_on_full_bearish_confluence():
    snapshot = _snapshot(close=90.0, ema_50=95.0, ema_200=100.0, adx_14=25.0, rsi_14=50.0,
                          market_structure_trend="DOWNTREND")
    signal = evaluate_trend_pullback(snapshot)
    assert signal.signal == "SHORT"


def test_trend_pullback_no_trade_when_adx_too_low():
    snapshot = _snapshot(close=110.0, ema_50=105.0, ema_200=100.0, adx_14=10.0, rsi_14=50.0,
                          market_structure_trend="UPTREND")
    assert evaluate_trend_pullback(snapshot).signal == "NO_TRADE"


def test_trend_pullback_no_trade_when_rsi_outside_pullback_band():
    snapshot = _snapshot(close=110.0, ema_50=105.0, ema_200=100.0, adx_14=25.0, rsi_14=85.0,
                          market_structure_trend="UPTREND")
    assert evaluate_trend_pullback(snapshot).signal == "NO_TRADE"


def test_trend_pullback_no_trade_when_structure_disagrees_with_ema_alignment():
    # bullish EMA alignment but structure says RANGING, not UPTREND - no confluence
    snapshot = _snapshot(close=110.0, ema_50=105.0, ema_200=100.0, adx_14=25.0, rsi_14=50.0,
                          market_structure_trend="RANGING")
    assert evaluate_trend_pullback(snapshot).signal == "NO_TRADE"


def test_trend_pullback_insufficient_data_when_ema200_missing():
    snapshot = _snapshot(close=110.0, ema_50=105.0, ema_200=None, adx_14=25.0, rsi_14=50.0)
    signal = evaluate_trend_pullback(snapshot)
    assert signal.signal == "NO_TRADE"
    assert "INSUFFICIENT_DATA" in signal.reasons


def test_trend_pullback_no_data_snapshot_never_raises():
    snapshot = TechnicalSnapshot(symbol="BTCUSDT", interval="1h", as_of=None, data_points=0, quality="NO_DATA")
    signal = evaluate_trend_pullback(snapshot)
    assert signal.signal == "NO_TRADE"


# -- breakout -------------------------------------------------------------------

def test_breakout_long_on_confirmed_breakout_with_volume():
    snapshot = _snapshot(breakout=True, breakdown=False, volume_zscore_20=2.0, last_swing_high=120.0)
    signal = evaluate_breakout(snapshot)
    assert signal.signal == "LONG"
    assert signal.strategy_id == STRATEGY_BREAKOUT


def test_breakout_short_on_confirmed_breakdown_with_volume():
    snapshot = _snapshot(breakout=False, breakdown=True, volume_zscore_20=2.0, last_swing_low=80.0)
    signal = evaluate_breakout(snapshot)
    assert signal.signal == "SHORT"


def test_breakout_no_trade_without_volume_confirmation():
    snapshot = _snapshot(breakout=True, breakdown=False, volume_zscore_20=0.2)
    signal = evaluate_breakout(snapshot)
    assert signal.signal == "NO_TRADE"
    assert "volume not confirmed" in signal.reasons


def test_breakout_no_trade_when_no_structural_break():
    snapshot = _snapshot(breakout=False, breakdown=False, volume_zscore_20=3.0)
    assert evaluate_breakout(snapshot).signal == "NO_TRADE"


def test_breakout_never_fires_on_volume_alone_without_a_break():
    # spec section 49: a volume spike by itself must never be sufficient
    snapshot = _snapshot(breakout=False, breakdown=False, volume_zscore_20=5.0)
    assert evaluate_breakout(snapshot).signal == "NO_TRADE"


# -- mean reversion ---------------------------------------------------------------

def test_mean_reversion_short_on_overbought_ranging_extended_above_vwap():
    snapshot = _snapshot(close=103.0, rsi_14=75.0, adx_14=15.0, distance_from_vwap_pct=3.0)
    signal = evaluate_mean_reversion(snapshot)
    assert signal.signal == "SHORT"
    assert signal.strategy_id == STRATEGY_MEAN_REVERSION


def test_mean_reversion_long_on_oversold_ranging_extended_below_vwap():
    snapshot = _snapshot(close=97.0, rsi_14=25.0, adx_14=15.0, distance_from_vwap_pct=-3.0)
    signal = evaluate_mean_reversion(snapshot)
    assert signal.signal == "LONG"


def test_mean_reversion_never_fires_on_rsi_alone_in_a_trending_market():
    # spec section 51: "Nunca vender somente porque RSI > 70" - here ADX is
    # high (trending), so overbought RSI must NOT trigger a reversion short
    snapshot = _snapshot(close=103.0, rsi_14=80.0, adx_14=35.0, distance_from_vwap_pct=3.0)
    assert evaluate_mean_reversion(snapshot).signal == "NO_TRADE"


def test_mean_reversion_no_trade_when_rsi_extreme_but_vwap_distance_small():
    snapshot = _snapshot(close=100.5, rsi_14=75.0, adx_14=15.0, distance_from_vwap_pct=0.5)
    assert evaluate_mean_reversion(snapshot).signal == "NO_TRADE"


def test_mean_reversion_no_trade_in_the_middle_of_the_range():
    snapshot = _snapshot(close=100.1, rsi_14=50.0, adx_14=15.0, distance_from_vwap_pct=0.1)
    assert evaluate_mean_reversion(snapshot).signal == "NO_TRADE"


# -- evaluate_all ---------------------------------------------------------------

def test_evaluate_all_returns_one_signal_per_strategy():
    snapshot = _snapshot(close=110.0, ema_50=105.0, ema_200=100.0, adx_14=25.0, rsi_14=50.0,
                          market_structure_trend="UPTREND", breakout=False, breakdown=False,
                          volume_zscore_20=0.0, distance_from_vwap_pct=0.0)
    signals = evaluate_all(snapshot)
    assert len(signals) == 3
    assert {s.strategy_id for s in signals} == {STRATEGY_TREND_PULLBACK, STRATEGY_BREAKOUT, STRATEGY_MEAN_REVERSION}


def test_evaluate_all_ignores_news_status_when_event_reaction_not_requested():
    # Regression: evaluate_all's new news_status param must be a no-op for
    # every engine that doesn't explicitly opt into EVENT_REACTION - it is
    # deliberately excluded from ALL_STRATEGY_IDS (see strategies.py's
    # module docstring), so passing news_status must never change anything
    # for existing callers.
    snapshot = _snapshot(close=110.0, ema_50=105.0, ema_200=100.0, adx_14=25.0, rsi_14=50.0,
                          market_structure_trend="UPTREND", breakout=False, breakdown=False,
                          volume_zscore_20=0.0, distance_from_vwap_pct=0.0)
    confirmed = AssetNewsStatus(asset="BTC", status="CONFIRMED", distinct_sources=3,
                                 item_count=5, dominant_sentiment="positive")
    signals = evaluate_all(snapshot, news_status=confirmed)
    assert len(signals) == 3
    assert STRATEGY_EVENT_REACTION not in {s.strategy_id for s in signals}


def test_evaluate_all_includes_event_reaction_when_explicitly_requested():
    snapshot = _snapshot(close=110.0, roc_12=5.0)
    confirmed = AssetNewsStatus(asset="BTC", status="CONFIRMED", distinct_sources=3,
                                 item_count=5, dominant_sentiment="positive")
    signals = evaluate_all(snapshot, strategy_ids=(STRATEGY_EVENT_REACTION,), news_status=confirmed)
    assert len(signals) == 1
    assert signals[0].strategy_id == STRATEGY_EVENT_REACTION
    assert signals[0].signal == "LONG"


# -- evaluate_event_reaction ------------------------------------------------

def test_event_reaction_long_on_confirmed_positive_news_and_rising_price():
    snapshot = _snapshot(close=110.0, roc_12=3.5)
    status = AssetNewsStatus(asset="BTC", status="CONFIRMED", distinct_sources=3,
                              item_count=4, dominant_sentiment="positive")
    signal = evaluate_event_reaction(snapshot, status)
    assert signal.signal == "LONG"
    assert signal.strength == pytest.approx(75.0)  # 3 sources * 25
    assert signal.strategy_id == STRATEGY_EVENT_REACTION


def test_event_reaction_short_on_confirmed_negative_news_and_falling_price():
    snapshot = _snapshot(close=90.0, roc_12=-4.0)
    status = AssetNewsStatus(asset="BTC", status="CONFIRMED", distinct_sources=2,
                              item_count=2, dominant_sentiment="negative")
    signal = evaluate_event_reaction(snapshot, status)
    assert signal.signal == "SHORT"
    assert signal.strength == pytest.approx(50.0)  # 2 sources * 25


def test_event_reaction_no_trade_when_news_status_is_none():
    snapshot = _snapshot(close=110.0, roc_12=5.0)
    signal = evaluate_event_reaction(snapshot, None)
    assert signal.signal == "NO_TRADE"


@pytest.mark.parametrize("status", ["NEWS_CONFLICT", "WAIT_FOR_CONFIRMATION", "NO_NEWS"])
def test_event_reaction_never_trades_on_unconfirmed_or_conflicting_news(status):
    snapshot = _snapshot(close=110.0, roc_12=5.0)
    news_status = AssetNewsStatus(asset="BTC", status=status, distinct_sources=1,
                                   item_count=1, dominant_sentiment="positive")
    signal = evaluate_event_reaction(snapshot, news_status)
    assert signal.signal == "NO_TRADE"


def test_event_reaction_no_trade_when_price_has_not_confirmed_direction_yet():
    # Confirmed positive news, but price hasn't actually moved up - the
    # whole point of the second condition is to refuse exactly this case.
    snapshot = _snapshot(close=110.0, roc_12=-1.0)
    status = AssetNewsStatus(asset="BTC", status="CONFIRMED", distinct_sources=3,
                              item_count=3, dominant_sentiment="positive")
    signal = evaluate_event_reaction(snapshot, status)
    assert signal.signal == "NO_TRADE"


def test_event_reaction_no_trade_on_neutral_dominant_sentiment():
    snapshot = _snapshot(close=110.0, roc_12=5.0)
    status = AssetNewsStatus(asset="BTC", status="CONFIRMED", distinct_sources=2,
                              item_count=2, dominant_sentiment="neutral")
    signal = evaluate_event_reaction(snapshot, status)
    assert signal.signal == "NO_TRADE"


def test_event_reaction_no_trade_without_roc_data():
    snapshot = _snapshot(close=110.0, roc_12=None)
    status = AssetNewsStatus(asset="BTC", status="CONFIRMED", distinct_sources=3,
                              item_count=3, dominant_sentiment="positive")
    signal = evaluate_event_reaction(snapshot, status)
    assert signal.signal == "NO_TRADE"
    assert "INSUFFICIENT_DATA" in signal.reasons
