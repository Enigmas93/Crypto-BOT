from datetime import date, timedelta

import pandas as pd
import pytest

from aegis.macro.models import MacroObservation
from aegis.macro.series_config import MacroSeriesConfig
from aegis.macro.service import MacroEngine, compute_snapshot


def _config(frequency="monthly", source="fred", series_id="FEDFUNDS") -> MacroSeriesConfig:
    return MacroSeriesConfig(series_id=series_id, name="Fed Funds", description="test",
                              frequency=frequency, source=source, importance="high", transformation="level")


def _observations_df(values: list[float], start: date = date(2020, 1, 1)) -> pd.DataFrame:
    dates = [start + timedelta(days=30 * i) for i in range(len(values))]
    return pd.DataFrame({"date": dates, "value": values})


def test_no_data_on_empty_observations():
    snapshot = compute_snapshot(_config(), pd.DataFrame())
    assert snapshot.quality == "NO_DATA"
    assert snapshot.as_of is None


def test_change_pct_matches_hand_computed_value():
    df = _observations_df([5.0, 5.5])
    snapshot = compute_snapshot(_config(), df)
    assert snapshot.change_pct == pytest.approx(10.0)


def test_yoy_pct_change_uses_frequency_specific_lookback():
    # monthly -> 12 periods back; 13 points means the YoY comparison is
    # index[-13] vs index[-1]
    values = [100.0] * 12 + [112.0]
    df = _observations_df(values)
    snapshot = compute_snapshot(_config(frequency="monthly"), df)
    assert snapshot.yoy_pct_change == pytest.approx(12.0)


def test_yoy_pct_change_none_without_a_full_year_of_history():
    df = _observations_df([100.0, 101.0, 102.0])
    snapshot = compute_snapshot(_config(frequency="monthly"), df)
    assert snapshot.yoy_pct_change is None


def test_quality_partial_vs_ok_threshold():
    partial = compute_snapshot(_config(), _observations_df([5.0] * 10))
    full = compute_snapshot(_config(), _observations_df([5.0] * 25))
    assert partial.quality == "PARTIAL_HISTORY"
    assert full.quality == "OK"


def test_to_features_dict_excludes_identity_and_none():
    df = _observations_df([5.0, 5.5])
    snapshot = compute_snapshot(_config(), df)
    features = snapshot.to_features_dict()
    for key in ("series_id", "as_of", "data_points", "quality"):
        assert key not in features
    assert "value" in features
    assert "yoy_pct_change" not in features  # None with only 2 points


class _FakeProvider:
    def __init__(self, observations_by_series: dict[str, list[dict]]):
        self._data = observations_by_series
        self.calls: list[str] = []

    async def get_series(self, series_id, **kwargs):
        self.calls.append(series_id)
        return self._data.get(series_id, [])


class _FakeRepo:
    def __init__(self, df_by_series: dict[str, pd.DataFrame] | None = None):
        self.registered: list = []
        self.observations_inserted: list[MacroObservation] = []
        self.snapshots_written: list = []
        self._df_by_series = df_by_series or {}

    async def upsert_series_registry(self, configs):
        self.registered.extend(configs)

    async def insert_observations(self, observations):
        self.observations_inserted.extend(observations)

    async def fetch_observations(self, series_id, limit=260):
        return self._df_by_series.get(series_id, pd.DataFrame(columns=["date", "value"]))

    async def upsert_snapshot(self, snapshot):
        self.snapshots_written.append(snapshot)


class _Settings:
    macro_history_limit = 260


@pytest.mark.asyncio
async def test_poll_and_store_one_filters_missing_values_and_stores_snapshot():
    provider = _FakeProvider({
        "FEDFUNDS": [{"date": "2026-01-01", "value": "5.33"}, {"date": "2026-02-01", "value": "."}],
    })
    df_after_insert = _observations_df([5.33])
    repo = _FakeRepo({"FEDFUNDS": df_after_insert})
    engine = MacroEngine({"fred": provider}, repo, _Settings(), [_config()])

    snapshot = await engine.poll_and_store_one(_config())

    assert len(repo.observations_inserted) == 1  # the "." row was dropped
    assert repo.observations_inserted[0].value == pytest.approx(5.33)
    assert len(repo.snapshots_written) == 1
    assert snapshot.value == pytest.approx(5.33)


@pytest.mark.asyncio
async def test_poll_and_store_one_routes_bls_series_to_the_bls_parser():
    provider = _FakeProvider({
        "LNS14000000": [
            {"year": "2026", "period": "M08", "periodName": "August", "value": "4.1"},
            {"year": "2026", "period": "M13", "periodName": "Annual", "value": "4.0"},  # skipped, not a real bar
        ],
    })
    df_after_insert = _observations_df([4.1])
    repo = _FakeRepo({"LNS14000000": df_after_insert})
    bls_config = _config(series_id="LNS14000000", source="bls")
    engine = MacroEngine({"bls": provider}, repo, _Settings(), [bls_config])

    snapshot = await engine.poll_and_store_one(bls_config)

    assert len(repo.observations_inserted) == 1  # M13 annual-average row dropped
    assert repo.observations_inserted[0].source == "bls"
    assert repo.observations_inserted[0].date == date(2026, 8, 1)
    assert snapshot.value == pytest.approx(4.1)


class _FakeBeaProvider:
    def __init__(self, rows: list[dict]):
        self._rows = rows
        self.calls: list[dict] = []

    async def get_series(self, series_id, dataset_name, table_name, frequency, years=None):
        self.calls.append({"series_id": series_id, "dataset_name": dataset_name,
                            "table_name": table_name, "frequency": frequency})
        return self._rows


@pytest.mark.asyncio
async def test_poll_and_store_one_passes_dataset_table_frequency_to_bea():
    bea_config = MacroSeriesConfig(series_id="A191RL", name="Real GDP", description="test",
                                    frequency="quarterly", source="bea", importance="high",
                                    transformation="level", dataset="NIPA", table="T10101", bea_frequency="Q")
    provider = _FakeBeaProvider([{"TimePeriod": "2026Q1", "DataValue": "3.4"}])
    repo = _FakeRepo({"A191RL": _observations_df([3.4])})
    engine = MacroEngine({"bea": provider}, repo, _Settings(), [bea_config])

    snapshot = await engine.poll_and_store_one(bea_config)

    assert provider.calls == [{"series_id": "A191RL", "dataset_name": "NIPA",
                                "table_name": "T10101", "frequency": "Q"}]
    assert repo.observations_inserted[0].source == "bea"
    assert snapshot.value == pytest.approx(3.4)


@pytest.mark.asyncio
async def test_poll_and_store_one_raises_for_a_source_with_no_registered_provider():
    engine = MacroEngine({"fred": _FakeProvider({})}, _FakeRepo(), _Settings(),
                          [_config(source="bea")])  # no "bea" provider wired up
    with pytest.raises(ValueError, match="no provider configured"):
        await engine.poll_and_store_one(_config(source="bea"))


@pytest.mark.asyncio
async def test_run_once_survives_one_series_failing():
    class _FlakyProvider(_FakeProvider):
        async def get_series(self, series_id, **kwargs):
            raise RuntimeError("FRED is down")

    configs = [_config(), MacroSeriesConfig(series_id="DGS10", name="10Y", description="t",
                                             frequency="daily", source="fred", importance="high",
                                             transformation="level")]
    engine = MacroEngine({"fred": _FlakyProvider({})}, _FakeRepo(), _Settings(), configs)

    results = await engine.run_once()  # must not raise

    assert results == {}


@pytest.mark.asyncio
async def test_run_once_handles_mixed_fred_and_bls_series_in_one_pass():
    fred_provider = _FakeProvider({"FEDFUNDS": [{"date": "2026-01-01", "value": "5.0"}]})
    bls_provider = _FakeProvider({"LNS14000000": [{"year": "2026", "period": "M01", "value": "4.0"}]})
    repo = _FakeRepo({
        "FEDFUNDS": _observations_df([5.0]),
        "LNS14000000": _observations_df([4.0]),
    })
    engine = MacroEngine(
        {"fred": fred_provider, "bls": bls_provider}, repo, _Settings(),
        [_config(), _config(series_id="LNS14000000", source="bls")],
    )

    results = await engine.run_once()

    assert set(results.keys()) == {"FEDFUNDS", "LNS14000000"}
    assert fred_provider.calls == ["FEDFUNDS"]
    assert bls_provider.calls == ["LNS14000000"]
