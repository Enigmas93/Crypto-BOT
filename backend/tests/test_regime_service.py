"""Unit tests for aegis.regime.service (Fase 17 - RegimeEngine). Pure
function, synthetic TechnicalSnapshot inputs, no I/O.
"""
from __future__ import annotations

from datetime import UTC, datetime

from aegis.regime.service import (
    HIGH_VOLATILITY,
    LOW_VOLATILITY,
    NORMAL_VOLATILITY,
    RANGING,
    TREND_DOWN,
    TREND_UP,
    UNKNOWN_TREND,
    classify_regime,
)
from aegis.technical.service import TechnicalSnapshot

_AS_OF = datetime(2026, 9, 21, tzinfo=UTC)


def _snapshot(**overrides) -> TechnicalSnapshot:
    base = dict(symbol="BTCUSDT", interval="1h", as_of=_AS_OF, data_points=300, quality="OK")
    base.update(overrides)
    return TechnicalSnapshot(**base)


def test_trending_up_on_high_adx_and_bullish_ema_alignment():
    snapshot = _snapshot(adx_14=30.0, close=110.0, ema_50=105.0, ema_200=100.0)
    regime = classify_regime(snapshot)
    assert regime.trend_regime == TREND_UP


def test_trending_down_on_high_adx_and_bearish_ema_alignment():
    snapshot = _snapshot(adx_14=30.0, close=90.0, ema_50=95.0, ema_200=100.0)
    regime = classify_regime(snapshot)
    assert regime.trend_regime == TREND_DOWN


def test_ranging_on_low_adx():
    snapshot = _snapshot(adx_14=12.0, close=100.0, ema_50=100.5, ema_200=99.5)
    regime = classify_regime(snapshot)
    assert regime.trend_regime == RANGING


def test_unknown_when_adx_high_but_emas_disagree_on_direction():
    # ADX says trending, but close/EMA order isn't a clean bullish or
    # bearish alignment - must not be forced into either bucket.
    snapshot = _snapshot(adx_14=30.0, close=101.0, ema_50=100.0, ema_200=102.0)
    regime = classify_regime(snapshot)
    assert regime.trend_regime == UNKNOWN_TREND


def test_unknown_when_adx_between_range_and_trend_thresholds():
    snapshot = _snapshot(adx_14=22.5, close=110.0, ema_50=105.0, ema_200=100.0)
    regime = classify_regime(snapshot)
    assert regime.trend_regime == UNKNOWN_TREND


def test_unknown_when_required_fields_are_missing():
    snapshot = _snapshot(adx_14=None, close=110.0, ema_50=105.0, ema_200=100.0)
    regime = classify_regime(snapshot)
    assert regime.trend_regime == UNKNOWN_TREND


def test_high_volatility_regime():
    # volatility_percentile_100 is a 0-1 FRACTION (see regime/service.py's
    # DEFAULT_HIGH_VOL_PERCENTILE comment) - NOT a 0-100 scale despite the
    # field's name; 0.95 means "higher than 95% of the trailing 100 bars".
    snapshot = _snapshot(volatility_percentile_100=0.95)
    regime = classify_regime(snapshot)
    assert regime.volatility_regime == HIGH_VOLATILITY


def test_low_volatility_regime():
    snapshot = _snapshot(volatility_percentile_100=0.05)
    regime = classify_regime(snapshot)
    assert regime.volatility_regime == LOW_VOLATILITY


def test_normal_volatility_regime():
    snapshot = _snapshot(volatility_percentile_100=0.50)
    regime = classify_regime(snapshot)
    assert regime.volatility_regime == NORMAL_VOLATILITY


def test_volatility_regime_is_none_without_data():
    snapshot = _snapshot(volatility_percentile_100=None)
    regime = classify_regime(snapshot)
    assert regime.volatility_regime is None


def test_regime_snapshot_carries_symbol_interval_and_as_of_through():
    snapshot = _snapshot(symbol="ETHUSDT", interval="4h", adx_14=30.0, close=110.0, ema_50=105.0, ema_200=100.0)
    regime = classify_regime(snapshot)
    assert regime.symbol == "ETHUSDT"
    assert regime.interval == "4h"
    assert regime.as_of == _AS_OF


def test_custom_thresholds_are_respected():
    snapshot = _snapshot(adx_14=18.0, close=110.0, ema_50=105.0, ema_200=100.0)
    # Default range threshold (20) would call this RANGING; a stricter
    # custom threshold should not.
    regime = classify_regime(snapshot, adx_range_threshold=15.0)
    assert regime.trend_regime == UNKNOWN_TREND
