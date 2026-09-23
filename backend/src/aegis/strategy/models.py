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
