"""Loads `macro_series.yaml` (spec section 33) - the single source of
truth for which series exist, never a series_id assumed inline in code."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(slots=True)
class MacroSeriesConfig:
    series_id: str
    name: str
    description: str
    frequency: str  # daily | weekly | monthly | quarterly | annual
    source: str
    importance: str  # high | medium | low
    transformation: str  # informational - see macro_series.yaml header

    # BEA-only: identifies which NIPA table/line this series comes from -
    # BEA has no single flat "series ID" concept like FRED/BLS, a value is
    # only addressable as (dataset, table, series_id within that table).
    # None for fred/bls entries.
    dataset: str | None = None
    table: str | None = None
    bea_frequency: str | None = None  # BEA's own frequency code: "Q" | "A" | "M"


def load_macro_series(path: str | Path) -> list[MacroSeriesConfig]:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    entries = raw.get("series", [])
    return [MacroSeriesConfig(**entry) for entry in entries]
