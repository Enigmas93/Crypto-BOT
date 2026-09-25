"""Shared strategy output type - spec section 47."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

Signal = Literal["LONG", "SHORT", "NO_TRADE"]


@dataclass(slots=True)
class StrategySignal:
    strategy_id: str
    symbol: str
    interval: str
    as_of: datetime | None
    signal: Signal
    strength: float  # 0-100, this strategy's own conviction - not yet combined with others
    reasons: list[str]
    # True only when this strategy had NO real opinion to give - required
    # input data was simply missing (e.g. LIQUIDATION_SQUEEZE with no
    # derivatives/liquidation snapshot on an account that doesn't collect
    # it). False for a genuine NO_TRADE where the strategy DID look at real
    # data and decided conditions don't align - that is real information,
    # not an abstention. Fase 17h: found live (2026-09-25) that
    # ConfluenceEngine.combine_signals divides by the total weight of every
    # signal handed to it, so adding a 4th strategy that almost always
    # abstains (LIQUIDATION_SQUEEZE, historically starved of real
    # liquidation volume on Binance testnet, and permanently starved on
    # BingX/Momentum) silently diluted every OTHER strategy's vote by ~25%
    # on every single symbol/account, including ones where the new
    # strategy could never contribute a real opinion at all - net_score
    # dropped from 56.7 to 42.5 on an identical 2-of-3 LONG alignment in a
    # reproduction, enough to fall below the 30.0 decision_threshold and
    # silence real entries for hours. `insufficient_data=True` signals are
    # excluded entirely from combine_signals' weighted average.
    insufficient_data: bool = False
