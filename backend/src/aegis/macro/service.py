"""MacroEngine - spec sections 32-36.

`compute_snapshot` is deliberately NOT `MacroSurpriseEngine` as the spec
literally describes it (actual vs. consensus *forecast*). Neither FRED nor
BLS publishes consensus forecasts - those are normally a paid data product
(Bloomberg/Reuters surveys). Rather than fabricate a "forecast" field, this
computes an honest substitute: how far the latest observation sits from
its own recent history (`zscore_vs_trailing`) plus period-over-period and
year-over-year change. A true forecast-based surprise engine is future
work, gated on a real forecast data source (spec section 105: don't add a
paid API until the free option's ceiling is proven and documented - this
docstring is that documentation).

One engine serves every source: `MacroSeriesConfig.source` ("fred", "bls",
...) picks both the provider and the payload parser for each series - see
`MacroEngine`/`_PARSERS` below.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date

import pandas as pd

from aegis import stats
from aegis.logging_utils import get_logger, log_event
from aegis.macro.models import MacroObservation
from aegis.macro.series_config import MacroSeriesConfig

_LOG = get_logger("macro.service")

MIN_POINTS_FOR_FULL_HISTORY = 20

# Approximate observations-per-year by native frequency, for the
# year-over-year lookback. Approximate because FRED daily series only have
# business-day observations (holidays/weekends create gaps) - documented,
# not hidden.
YOY_LOOKBACK_PERIODS = {"daily": 252, "weekly": 52, "monthly": 12, "quarterly": 4, "annual": 1}


@dataclass(slots=True)
class MacroSnapshot:
    series_id: str
    as_of: date | None
    data_points: int
    quality: str  # NO_DATA | PARTIAL_HISTORY | OK

    value: float | None = None
    change_pct: float | None = None  # vs the previous observation, native frequency
    yoy_pct_change: float | None = None
    zscore_vs_trailing: float | None = None

    def to_features_dict(self) -> dict:
        d = asdict(self)
        for key in ("series_id", "as_of", "data_points", "quality"):
            d.pop(key, None)
        return {k: v for k, v in d.items() if v is not None}


def compute_snapshot(config: MacroSeriesConfig, observations_df: pd.DataFrame) -> MacroSnapshot:
    if observations_df.empty:
        return MacroSnapshot(series_id=config.series_id, as_of=None, data_points=0, quality="NO_DATA")

    values = observations_df["value"]
    yoy_periods = YOY_LOOKBACK_PERIODS.get(config.frequency, 12)
    quality = "OK" if len(observations_df) >= MIN_POINTS_FOR_FULL_HISTORY else "PARTIAL_HISTORY"

    return MacroSnapshot(
        series_id=config.series_id,
        as_of=observations_df["date"].iloc[-1],
        data_points=len(observations_df),
        quality=quality,
        value=float(values.iloc[-1]),
        change_pct=stats.latest_pct_change(values),
        yoy_pct_change=stats.pct_change_over(values, periods=yoy_periods),
        zscore_vs_trailing=stats.latest_zscore(values, min_points=5),
    )


_PARSERS = {
    "fred": MacroObservation.from_fred_payload,
    "bls": MacroObservation.from_bls_payload,
    "bea": MacroObservation.from_bea_payload,
}


class MacroEngine:
    """`providers` maps a `MacroSeriesConfig.source` value ("fred", "bls",
    "bea", ...) to the provider instance that serves it - one engine polls
    every configured series regardless of which source it comes from,
    routing each to the right provider and the right raw-payload parser."""

    def __init__(self, providers: dict[str, object], repository, settings,
                 series_configs: list[MacroSeriesConfig]) -> None:
        self.providers = providers
        self.repo = repository
        self.settings = settings
        self.series_configs = series_configs

    async def _fetch_raw(self, provider, config: MacroSeriesConfig) -> list[dict]:
        """Every source but BEA is addressable by `series_id` alone. BEA has
        no flat series id - a value only exists as (dataset, table, a
        SeriesCode within that table) - so it is the one provider this
        engine calls with the extra context `MacroSeriesConfig` carries."""
        if config.source == "bea":
            return await provider.get_series(
                config.series_id, dataset_name=config.dataset, table_name=config.table,
                frequency=config.bea_frequency,
            )
        return await provider.get_series(config.series_id)

    async def poll_and_store_one(self, config: MacroSeriesConfig) -> MacroSnapshot:
        await self.repo.upsert_series_registry([config])

        provider = self.providers.get(config.source)
        if provider is None:
            raise ValueError(f"no provider configured for source '{config.source}' ({config.series_id})")
        parse = _PARSERS.get(config.source)
        if parse is None:
            raise ValueError(f"no parser registered for source '{config.source}' ({config.series_id})")

        raw = await self._fetch_raw(provider, config)
        observations = [obs for obs in (parse(config.series_id, row) for row in raw) if obs is not None]
        # Macro data gets revised after initial release (GDP, CPI, payrolls
        # are all restated later) - insert_observations upserts, it does
        # not assume this data is immutable the way a filled trade is.
        await self.repo.insert_observations(observations)

        df = await self.repo.fetch_observations(config.series_id, limit=self.settings.macro_history_limit)
        snapshot = compute_snapshot(config, df)
        await self.repo.upsert_snapshot(snapshot)
        return snapshot

    async def run_once(self) -> dict[str, MacroSnapshot]:
        results: dict[str, MacroSnapshot] = {}
        for config in self.series_configs:
            try:
                results[config.series_id] = await self.poll_and_store_one(config)
            except Exception as exc:  # noqa: BLE001 - one bad series must not skip the rest
                log_event(_LOG, "macro_poll_failed", level=40, series_id=config.series_id, error=str(exc))
        return results
