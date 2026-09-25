"""RegimeEngine (Fase 17) - market regime classification, a "feature" layer
in the same spirit as DerivativesSnapshot/LiquidationSnapshot (spec section
28: "Utilizar como features. Não interpretar automaticamente como sinal de
trade") - classifies the CURRENT bar's regime from indicators
TechnicalAnalysisService already computes, and produces no trade decision
on its own. Deliberately NOT wired into any strategy's gating logic here -
doing so would be a real behavior change to already-running Paper/Shadow/
Momentum engines, which needs its own validation (backtest/walk-forward)
before it should ever influence a live decision, not a silent side effect
of adding a classifier module.

Two independent axes, kept separate rather than collapsed into one number
that would hide which one is actually driving a reading:

  - Trend regime (ADX + EMA-50/200 alignment): TRENDING_UP / TRENDING_DOWN /
    RANGING / UNKNOWN (ADX doesn't clear either the trend or range
    threshold, or clears the trend threshold but the EMAs don't agree on a
    direction - reported honestly rather than forced into a bucket).
  - Volatility regime (`volatility_percentile_100`, already computed by
    TechnicalAnalysisService): HIGH_VOLATILITY / NORMAL_VOLATILITY /
    LOW_VOLATILITY.

Thresholds are a documented HYPOTHESIS (spec section 54's standard again,
not fitted): the ADX 25/20 trend/range bands mirror the same bands
`evaluate_trend_pullback`/`evaluate_mean_reversion` already use elsewhere
in this codebase, kept consistent rather than inventing new arbitrary
numbers for the same underlying indicator.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from aegis.technical.service import TechnicalSnapshot

TREND_UP = "TRENDING_UP"
TREND_DOWN = "TRENDING_DOWN"
RANGING = "RANGING"
UNKNOWN_TREND = "UNKNOWN"

HIGH_VOLATILITY = "HIGH_VOLATILITY"
NORMAL_VOLATILITY = "NORMAL_VOLATILITY"
LOW_VOLATILITY = "LOW_VOLATILITY"

DEFAULT_ADX_TREND_THRESHOLD = 25.0
DEFAULT_ADX_RANGE_THRESHOLD = 20.0
# `volatility_percentile_100` is a 0-1 FRACTION despite its name - the "100"
# refers to the 100-bar lookback window it's ranked against
# (aegis.technical.indicators.percentile_rank), not a 0-100 scale. Found
# live 2026-09-25: every real symbol read back well under 1.0, which a
# 0-100-scaled threshold would have silently misclassified as
# LOW_VOLATILITY 100% of the time.
DEFAULT_HIGH_VOL_PERCENTILE = 0.80
DEFAULT_LOW_VOL_PERCENTILE = 0.20


@dataclass(slots=True)
class RegimeSnapshot:
    symbol: str
    interval: str
    as_of: datetime | None
    trend_regime: str
    volatility_regime: str | None
    adx_14: float | None
    volatility_percentile_100: float | None


def classify_regime(
    snapshot: TechnicalSnapshot,
    adx_trend_threshold: float = DEFAULT_ADX_TREND_THRESHOLD,
    adx_range_threshold: float = DEFAULT_ADX_RANGE_THRESHOLD,
    high_vol_percentile: float = DEFAULT_HIGH_VOL_PERCENTILE,
    low_vol_percentile: float = DEFAULT_LOW_VOL_PERCENTILE,
) -> RegimeSnapshot:
    trend_regime = UNKNOWN_TREND
    required = (snapshot.adx_14, snapshot.close, snapshot.ema_50, snapshot.ema_200)
    if all(v is not None for v in required):
        if snapshot.adx_14 >= adx_trend_threshold:
            if snapshot.close > snapshot.ema_50 > snapshot.ema_200:
                trend_regime = TREND_UP
            elif snapshot.close < snapshot.ema_50 < snapshot.ema_200:
                trend_regime = TREND_DOWN
            # else: ADX says trending but EMAs don't agree on direction -
            # UNKNOWN_TREND stands, never guessed.
        elif snapshot.adx_14 <= adx_range_threshold:
            trend_regime = RANGING
        # else: ADX sits between the two thresholds - genuinely ambiguous.

    volatility_regime = None
    if snapshot.volatility_percentile_100 is not None:
        if snapshot.volatility_percentile_100 >= high_vol_percentile:
            volatility_regime = HIGH_VOLATILITY
        elif snapshot.volatility_percentile_100 <= low_vol_percentile:
            volatility_regime = LOW_VOLATILITY
        else:
            volatility_regime = NORMAL_VOLATILITY

    return RegimeSnapshot(
        symbol=snapshot.symbol, interval=snapshot.interval, as_of=snapshot.as_of,
        trend_regime=trend_regime, volatility_regime=volatility_regime,
        adx_14=snapshot.adx_14, volatility_percentile_100=snapshot.volatility_percentile_100,
    )
