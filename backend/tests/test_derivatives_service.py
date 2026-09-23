import pandas as pd
import pytest

from aegis.derivatives.service import DerivativesEngine, compute_snapshot
from aegis.providers.binance.models import LongShortRatioPoint, OpenInterestHistPoint


def _times(n: int) -> pd.Series:
    return pd.Series(pd.date_range("2026-01-01", periods=n, freq="5min", tz="UTC"))


def _oi_df(values: list[float]) -> pd.DataFrame:
    return pd.DataFrame({
        "time": _times(len(values)),
        "sum_open_interest": values,
        "sum_open_interest_value": [v * 50000 for v in values],
    })


def _ls_df(ratios: list[float]) -> pd.DataFrame:
    return pd.DataFrame({
        "time": _times(len(ratios)),
        "long_short_ratio": ratios,
        "long_account": [0.5] * len(ratios),
        "short_account": [0.5] * len(ratios),
    })


def _funding_df(rates: list[float], mark: float = 50100.0, index: float = 50000.0) -> pd.DataFrame:
    return pd.DataFrame({
        "event_time": _times(len(rates)),
        "mark_price": [mark] * len(rates),
        "index_price": [index] * len(rates),
        "funding_rate": rates,
    })


def _price_df(closes: list[float]) -> pd.DataFrame:
    return pd.DataFrame({
        "close": closes,
        "quote_volume": [1_000_000.0] * len(closes),
    })


def _empty(cols: list[str]) -> pd.DataFrame:
    return pd.DataFrame(columns=cols)


EMPTY_LS = _empty(["time", "long_short_ratio", "long_account", "short_account"])
EMPTY_FUNDING = _empty(["event_time", "mark_price", "index_price", "funding_rate"])
EMPTY_PRICE = _empty(["close", "quote_volume"])


def test_no_data_when_open_interest_is_empty():
    snapshot = compute_snapshot(
        "BTCUSDT", "5m", _empty(["time", "sum_open_interest", "sum_open_interest_value"]),
        EMPTY_LS, EMPTY_LS, EMPTY_LS, EMPTY_FUNDING, EMPTY_PRICE,
    )
    assert snapshot.quality == "NO_DATA"
    assert snapshot.as_of is None


@pytest.mark.parametrize(
    "oi_values,closes,expected_pattern",
    [
        ([100.0, 110.0], [50000.0, 51000.0], "PRICE_UP_OI_UP"),
        ([100.0, 90.0], [50000.0, 51000.0], "PRICE_UP_OI_DOWN"),
        ([100.0, 110.0], [50000.0, 49000.0], "PRICE_DOWN_OI_UP"),
        ([100.0, 90.0], [50000.0, 49000.0], "PRICE_DOWN_OI_DOWN"),
    ],
)
def test_price_oi_pattern_classification(oi_values, closes, expected_pattern):
    snapshot = compute_snapshot(
        "BTCUSDT", "5m", _oi_df(oi_values), EMPTY_LS, EMPTY_LS, EMPTY_LS, EMPTY_FUNDING, _price_df(closes),
    )
    assert snapshot.price_oi_pattern == expected_pattern


def test_price_oi_pattern_is_none_without_price_data():
    snapshot = compute_snapshot(
        "BTCUSDT", "5m", _oi_df([100.0, 110.0]), EMPTY_LS, EMPTY_LS, EMPTY_LS, EMPTY_FUNDING, EMPTY_PRICE,
    )
    assert snapshot.price_oi_pattern is None
    assert snapshot.price_change_pct is None


def test_basis_and_basis_pct_from_funding_series():
    snapshot = compute_snapshot(
        "BTCUSDT", "5m", _oi_df([100.0]), EMPTY_LS, EMPTY_LS, EMPTY_LS,
        _funding_df([0.0001], mark=50100.0, index=50000.0), EMPTY_PRICE,
    )
    assert snapshot.basis == pytest.approx(100.0)
    assert snapshot.basis_pct == pytest.approx(0.2)
    assert snapshot.funding_rate == pytest.approx(0.0001)


def test_long_short_ratios_populate_independently_per_type():
    snapshot = compute_snapshot(
        "BTCUSDT", "5m", _oi_df([100.0]),
        _ls_df([1.5]), _ls_df([2.0]), _ls_df([0.8]),
        EMPTY_FUNDING, EMPTY_PRICE,
    )
    assert snapshot.global_long_short_ratio == pytest.approx(1.5)
    assert snapshot.top_account_long_short_ratio == pytest.approx(2.0)
    assert snapshot.top_position_long_short_ratio == pytest.approx(0.8)


def test_quality_partial_history_below_threshold_ok_above():
    partial = compute_snapshot("BTCUSDT", "5m", _oi_df([1.0] * 10), EMPTY_LS, EMPTY_LS, EMPTY_LS,
                                EMPTY_FUNDING, EMPTY_PRICE)
    full = compute_snapshot("BTCUSDT", "5m", _oi_df([1.0] * 30), EMPTY_LS, EMPTY_LS, EMPTY_LS,
                             EMPTY_FUNDING, EMPTY_PRICE)
    assert partial.quality == "PARTIAL_HISTORY"
    assert full.quality == "OK"


def test_to_features_dict_excludes_identity_and_none():
    snapshot = compute_snapshot("BTCUSDT", "5m", _oi_df([100.0]), EMPTY_LS, EMPTY_LS, EMPTY_LS,
                                 EMPTY_FUNDING, EMPTY_PRICE)
    features = snapshot.to_features_dict()
    for key in ("symbol", "period", "as_of", "data_points", "quality"):
        assert key not in features
    assert "open_interest" in features
    assert "funding_rate" not in features  # was None - must not clobber another writer's value


class _FakeRest:
    def __init__(self):
        self.calls: list[tuple] = []

    async def get_open_interest_hist(self, symbol, period, limit):
        self.calls.append(("oi", symbol, period, limit))
        return [{"symbol": symbol, "sumOpenInterest": "100.0", "sumOpenInterestValue": "5000000.0",
                 "timestamp": 1700000000000}]

    async def get_global_long_short_ratio(self, symbol, period, limit):
        self.calls.append(("global", symbol, period, limit))
        return [{"symbol": symbol, "longShortRatio": "1.2", "longAccount": "0.55",
                  "shortAccount": "0.45", "timestamp": 1700000000000}]

    async def get_top_long_short_account_ratio(self, symbol, period, limit):
        self.calls.append(("top_account", symbol, period, limit))
        return [{"symbol": symbol, "longShortRatio": "1.4", "longAccount": "0.58",
                  "shortAccount": "0.42", "timestamp": 1700000000000}]

    async def get_top_long_short_position_ratio(self, symbol, period, limit):
        self.calls.append(("top_position", symbol, period, limit))
        return [{"symbol": symbol, "longShortRatio": "0.9", "longAccount": "0.47",
                  "shortAccount": "0.53", "timestamp": 1700000000000}]


class _FakeDerivativesRepo:
    def __init__(self):
        self.oi_written: list[OpenInterestHistPoint] = []
        self.ls_written: list[LongShortRatioPoint] = []

    async def insert_open_interest(self, points):
        self.oi_written.extend(points)

    async def insert_long_short_ratios(self, points):
        self.ls_written.extend(points)

    async def fetch_open_interest_series(self, symbol, period, limit):
        return _oi_df([100.0, 105.0])

    async def fetch_long_short_series(self, symbol, ratio_type, period, limit):
        return _ls_df([1.1])

    async def fetch_funding_series(self, symbol, limit):
        return EMPTY_FUNDING


class _FakeCandleRepo:
    async def fetch_ohlcv(self, symbol, interval, limit=500, closed_only=True):
        return EMPTY_PRICE


class _FakeFeatureRepo:
    def __init__(self):
        self.written = []

    async def upsert_snapshot(self, snapshot):
        self.written.append(snapshot)


class _Settings:
    symbols = ["BTCUSDT"]
    derivatives_periods = ["5m"]
    derivatives_hist_limit = 30
    funding_zscore_lookback = 100


@pytest.mark.asyncio
async def test_poll_and_persist_raw_writes_oi_and_all_three_ratio_types():
    rest = _FakeRest()
    repo = _FakeDerivativesRepo()
    engine = DerivativesEngine(rest, repo, _FakeCandleRepo(), _FakeFeatureRepo(), _Settings())

    await engine.poll_and_persist_raw("BTCUSDT", "5m")

    assert len(repo.oi_written) == 1
    assert repo.oi_written[0].sum_open_interest == pytest.approx(100.0)
    ratio_types = {p.ratio_type for p in repo.ls_written}
    assert ratio_types == {"GLOBAL_ACCOUNT", "TOP_ACCOUNT", "TOP_POSITION"}


@pytest.mark.asyncio
async def test_run_once_computes_and_stores_a_snapshot_per_symbol_period():
    rest = _FakeRest()
    feature_repo = _FakeFeatureRepo()
    engine = DerivativesEngine(rest, _FakeDerivativesRepo(), _FakeCandleRepo(), feature_repo, _Settings())

    results = await engine.run_once()

    assert set(results.keys()) == {"BTCUSDT:5m"}
    assert results["BTCUSDT:5m"].quality in {"OK", "PARTIAL_HISTORY"}
    assert len(feature_repo.written) == 1


@pytest.mark.asyncio
async def test_run_once_survives_one_symbol_failing():
    class _FlakyRest(_FakeRest):
        async def get_open_interest_hist(self, symbol, period, limit):
            raise RuntimeError("boom")

    class _TwoSymbolSettings(_Settings):
        symbols = ["BTCUSDT", "ETHUSDT"]

    engine = DerivativesEngine(_FlakyRest(), _FakeDerivativesRepo(), _FakeCandleRepo(),
                                _FakeFeatureRepo(), _TwoSymbolSettings())

    results = await engine.run_once()  # must not raise

    assert results == {}
