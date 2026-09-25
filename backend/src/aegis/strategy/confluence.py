"""ConfluenceEngine - spec sections 53/54/125.

Weighted voting across strategy signals, not a majority count (spec
section 125: "Não fazer simplesmente maioria 3x2"). Weights default to
equal (1.0 each) and are explicitly a starting hypothesis, not a validated
model - spec section 54 is direct about this: weights only earn their
values through backtesting. `BacktestEngine` (Phase 8) is what will
eventually tell us whether these defaults deserve to change.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from aegis.strategy.models import Signal, StrategySignal
from aegis.strategy.strategies import ALL_STRATEGY_IDS

DEFAULT_WEIGHTS: dict[str, float] = {strategy_id: 1.0 for strategy_id in ALL_STRATEGY_IDS}

_DIRECTION = {"LONG": 1.0, "SHORT": -1.0, "NO_TRADE": 0.0}


@dataclass(slots=True)
class ConfluenceResult:
    symbol: str
    interval: str
    as_of: datetime | None
    decision: Signal
    confluence_score: float  # 0-100, magnitude only - direction is `decision`
    net_score: float  # -100..100, signed - what `decision` was thresholded against
    strategy_signals: list[StrategySignal]


def combine_signals(
    signals: list[StrategySignal], weights: dict[str, float] | None = None, decision_threshold: float = 30.0,
) -> ConfluenceResult:
    if not signals:
        raise ValueError("combine_signals requires at least one StrategySignal")
    weights = weights or DEFAULT_WEIGHTS

    weighted_sum = 0.0
    total_weight = 0.0
    for s in signals:
        if s.insufficient_data:
            # An abstention, not a vote (Fase 17h) - a strategy with no
            # real opinion (missing required data) must not dilute every
            # other strategy's weight just by being present in
            # `strategy_ids`. Still returned in `strategy_signals` below
            # for transparency/logging, just excluded from the math.
            continue
        w = weights.get(s.strategy_id, 1.0)
        weighted_sum += _DIRECTION[s.signal] * s.strength * w
        total_weight += w

    net_score = weighted_sum / total_weight if total_weight > 0 else 0.0

    if net_score >= decision_threshold:
        decision: Signal = "LONG"
    elif net_score <= -decision_threshold:
        decision = "SHORT"
    else:
        decision = "NO_TRADE"

    first = signals[0]
    return ConfluenceResult(
        symbol=first.symbol, interval=first.interval, as_of=first.as_of,
        decision=decision, confluence_score=abs(net_score), net_score=net_score,
        strategy_signals=signals,
    )
