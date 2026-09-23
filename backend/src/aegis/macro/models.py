"""Typed representation of a single macro-economic observation.

One shared model across providers (FRED, BLS, ...) - same reasoning as
`Kline` having both `from_rest_row` and `from_ws_payload`: the canonical
shape is the same, only the raw wire format differs per source.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any


@dataclass(slots=True)
class MacroObservation:
    series_id: str
    date: date
    value: float
    source: str = "fred"

    @classmethod
    def from_fred_payload(cls, series_id: str, payload: dict[str, Any]) -> "MacroObservation | None":
        """FRED marks a missing observation with the literal string "." -
        that means no data, not zero. Returning None here (never a
        fabricated 0.0) is the spec-section-43 principle applied to macro
        data: an unavailable point stays unavailable."""
        raw_value = payload.get("value")
        if raw_value in (".", "", None):
            return None
        return cls(series_id=series_id, date=date.fromisoformat(payload["date"]), value=float(raw_value),
                    source="fred")

    @classmethod
    def from_bls_payload(cls, series_id: str, payload: dict[str, Any]) -> "MacroObservation | None":
        """BLS marks a suppressed/missing value in a few ways in practice;
        most reliably, a genuine observation always has a numeric-looking
        `value`. `period` "M13" is an annual average row BLS includes
        alongside the 12 monthly ones - skipped here since it would collide
        as a 13th data point for the same year with no real calendar date.
        Only monthly series (period "M01".."M12") are handled; quarterly/
        annual BLS series are out of scope until one is actually added to
        macro_series.yaml."""
        period = payload.get("period", "")
        if not (period.startswith("M") and period != "M13"):
            return None
        raw_value = payload.get("value")
        if raw_value in (None, "", "-"):
            return None
        try:
            month = int(period[1:])
            year = int(payload["year"])
            value = float(raw_value)
        except (ValueError, KeyError):
            return None
        return cls(series_id=series_id, date=date(year, month, 1), value=value, source="bls")

    @classmethod
    def from_bea_payload(cls, series_id: str, payload: dict[str, Any]) -> "MacroObservation | None":
        """BEA's `TimePeriod` is "2024Q1" (quarterly), "2024M01" (monthly)
        or a bare "2024" (annual) - mapped to that period's first calendar
        day. `DataValue` can be comma-formatted ("1,234.5"), unlike
        FRED/BLS."""
        time_period = payload.get("TimePeriod", "")
        raw_value = payload.get("DataValue")
        if not time_period or raw_value in (None, "", "(NA)"):
            return None
        try:
            if "Q" in time_period:
                year_str, q_str = time_period.split("Q")
                month = (int(q_str) - 1) * 3 + 1
            elif "M" in time_period:
                year_str, m_str = time_period.split("M")
                month = int(m_str)
            else:
                year_str, month = time_period, 1
            observation_date = date(int(year_str), month, 1)
            value = float(str(raw_value).replace(",", ""))
        except (ValueError, TypeError):
            return None
        return cls(series_id=series_id, date=observation_date, value=value, source="bea")
