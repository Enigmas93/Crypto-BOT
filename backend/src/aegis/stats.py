"""Small, pure statistics helpers shared across engines.

Started life as `derivatives/analytics.py` (Phase 4); promoted here once
`liquidation/service.py` (Phase 4b) started importing it too, and
`macro/service.py` (Phase 5) needed the same primitives again - three
independent domains computing "how has this series moved lately" is the
signal that this belongs at the top level, not owned by one engine.

Every function takes a bare `pd.Series` and returns a float or None (never
0, never a guess) when there isn't enough history - trivial to hand-verify
in isolation, no I/O, no state.
"""
from __future__ import annotations

import pandas as pd


def latest_pct_change(series: pd.Series) -> float | None:
    """% change from the second-to-last to the last value. None (never 0)
    when there aren't at least 2 points yet."""
    if len(series) < 2:
        return None
    prev, last = series.iloc[-2], series.iloc[-1]
    if prev == 0:
        return None
    return float((last - prev) / prev * 100)


def pct_change_over(series: pd.Series, periods: int) -> float | None:
    """% change from `periods` observations ago to the last value - the
    generalisation of `latest_pct_change` (which is `periods=1`). Used for
    e.g. a year-over-year change on a series sampled at a known frequency."""
    if periods < 1 or len(series) <= periods:
        return None
    prev, last = series.iloc[-1 - periods], series.iloc[-1]
    if prev == 0:
        return None
    return float((last - prev) / prev * 100)


def latest_zscore(series: pd.Series, min_points: int = 5) -> float | None:
    """How many standard deviations the last value is from the series'
    own mean. Population std (ddof=0) since this is a full-window
    description, not a sample estimate of some larger population."""
    if len(series) < min_points:
        return None
    std = series.std(ddof=0)
    if std == 0 or pd.isna(std):
        return None
    return float((series.iloc[-1] - series.mean()) / std)


def acceleration(series: pd.Series) -> float | None:
    """Change of the % change itself - needs 3 points (2 deltas)."""
    if len(series) < 3:
        return None
    delta_prev = latest_pct_change(series.iloc[:-1])
    delta_last = latest_pct_change(series)
    if delta_prev is None or delta_last is None:
        return None
    return delta_last - delta_prev
