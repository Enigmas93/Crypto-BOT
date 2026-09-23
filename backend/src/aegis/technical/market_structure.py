"""Swing-based market structure: HH/HL/LH/LL, breakout/breakdown (spec
section 25).

Swing points use a symmetric fractal (a bar is a swing high if it is the
local max across `left` bars before and `right` bars after it). This means
the most recent `right` bars can never be confirmed as swings yet - a
deliberate anti-lookahead property: today's bar cannot retroactively become
a "recent high" until enough bars have passed to confirm it, so breakout
detection is always comparing against a level that was real *before* this
bar closed.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import pandas as pd

Trend = Literal["UPTREND", "DOWNTREND", "RANGING", "UNKNOWN"]


@dataclass(slots=True)
class SwingPoint:
    index: int
    time: pd.Timestamp
    price: float
    kind: Literal["high", "low"]


@dataclass(slots=True)
class MarketStructureResult:
    trend: Trend
    last_swing_high: float | None
    last_swing_low: float | None
    breakout: bool
    breakdown: bool


def find_swing_points(df: pd.DataFrame, left: int = 2, right: int = 2) -> list[SwingPoint]:
    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    times = df["open_time"].to_numpy()
    n = len(df)
    points: list[SwingPoint] = []

    for i in range(left, n - right):
        # slices are always exactly `left`/`right` elements long here, never empty
        left_high, right_high = highs[i - left : i].max(), highs[i + 1 : i + 1 + right].max()
        if highs[i] >= left_high and highs[i] >= right_high and (highs[i] > left_high or highs[i] > right_high):
            points.append(SwingPoint(index=i, time=pd.Timestamp(times[i]), price=float(highs[i]), kind="high"))

        left_low, right_low = lows[i - left : i].min(), lows[i + 1 : i + 1 + right].min()
        if lows[i] <= left_low and lows[i] <= right_low and (lows[i] < left_low or lows[i] < right_low):
            points.append(SwingPoint(index=i, time=pd.Timestamp(times[i]), price=float(lows[i]), kind="low"))

    return points


def analyze_market_structure(df: pd.DataFrame, left: int = 2, right: int = 2) -> MarketStructureResult:
    if len(df) < left + right + 1:
        return MarketStructureResult("UNKNOWN", None, None, False, False)

    swings = find_swing_points(df, left=left, right=right)
    swing_highs = [s for s in swings if s.kind == "high"]
    swing_lows = [s for s in swings if s.kind == "low"]

    trend: Trend = "UNKNOWN"
    if len(swing_highs) >= 2 and len(swing_lows) >= 2:
        higher_high = swing_highs[-1].price > swing_highs[-2].price
        higher_low = swing_lows[-1].price > swing_lows[-2].price
        lower_high = swing_highs[-1].price < swing_highs[-2].price
        lower_low = swing_lows[-1].price < swing_lows[-2].price
        if higher_high and higher_low:
            trend = "UPTREND"
        elif lower_high and lower_low:
            trend = "DOWNTREND"
        else:
            trend = "RANGING"

    last_swing_high = swing_highs[-1].price if swing_highs else None
    last_swing_low = swing_lows[-1].price if swing_lows else None
    last_close = float(df["close"].iloc[-1])

    return MarketStructureResult(
        trend=trend,
        last_swing_high=last_swing_high,
        last_swing_low=last_swing_low,
        breakout=last_swing_high is not None and last_close > last_swing_high,
        breakdown=last_swing_low is not None and last_close < last_swing_low,
    )
