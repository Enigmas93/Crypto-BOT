import pandas as pd
import pytest

from aegis.liquidation.service import (
    LiquidationEngine,
    compute_snapshot,
    seconds_to_period_label,
)


def _times(n: int):
    return pd.date_range("2026-01-01", periods=n, freq="5min", tz="UTC")


def _windowed_df(long_vals: list[float], short_vals: list[float]) -> pd.DataFrame:
    n = len(long_vals)
    return pd.DataFrame({
        "bucket_time": _times(n),
        "long_notional": long_vals,
        "short_notional": short_vals,
        "long_count": [max(1, int(v > 0)) for v in long_vals],
        "short_count": [max(1, int(v > 0)) for v in short_vals],
    })


def test_seconds_to_period_label():
    assert seconds_to_period_label(300) == "5m"
    assert seconds_to_period_label(3600) == "1h"
    assert seconds_to_period_label(86400) == "1d"
    assert seconds_to_period_label(90) == "90s"


def test_no_data_on_empty_window():
    snapshot = compute_snapshot("BTCUSDT", "5m", pd.DataFrame())
    assert snapshot.quality == "NO_DATA"
    assert snapshot.as_of is None


def test_imbalance_is_positive_one_when_only_longs_liquidated():
    df = _windowed_df([1000.0] * 6, [0.0] * 6)
    snapshot = compute_snapshot("BTCUSDT", "5m", df)
    assert snapshot.liquidation_imbalance == pytest.approx(1.0)


def test_imbalance_is_negative_one_when_only_shorts_liquidated():
    df = _windowed_df([0.0] * 6, [1000.0] * 6)
    snapshot = compute_snapshot("BTCUSDT", "5m", df)
    assert snapshot.liquidation_imbalance == pytest.approx(-1.0)


def test_imbalance_is_none_when_no_liquidations_in_latest_bucket():
    df = _windowed_df([500.0, 500.0, 0.0], [500.0, 500.0, 0.0])
    snapshot = compute_snapshot("BTCUSDT", "5m", df)
    assert snapshot.liquidation_imbalance is None


def test_quality_partial_vs_ok_threshold():
    partial = compute_snapshot("BTCUSDT", "5m", _windowed_df([10.0] * 5, [0.0] * 5))
    full = compute_snapshot("BTCUSDT", "5m", _windowed_df([10.0] * 20, [0.0] * 20))
    assert partial.quality == "PARTIAL_HISTORY"
    assert full.quality == "OK"


def test_squeeze_score_computes_from_whichever_components_are_available():
    # 1 bucket: no z-score (needs >=5 points), no acceleration (needs >=3),
    # but imbalance IS available from this single bucket alone.
    df = _windowed_df([100.0], [0.0])
    snapshot = compute_snapshot("BTCUSDT", "5m", df)
    assert snapshot.liquidation_notional_zscore is None
    assert snapshot.liquidation_acceleration_pct is None
    assert snapshot.squeeze_score == pytest.approx(100.0)  # imbalance=1.0 is the only input -> full weight


def test_squeeze_score_higher_for_a_sudden_one_sided_spike_than_steady_balanced_flow():
    steady = _windowed_df([100.0] * 20, [100.0] * 20)  # balanced, no spike
    spike = _windowed_df([100.0] * 19 + [5000.0], [0.0] * 20)  # one-sided spike at the end

    steady_score = compute_snapshot("BTCUSDT", "5m", steady).squeeze_score
    spike_score = compute_snapshot("BTCUSDT", "5m", spike).squeeze_score

    assert spike_score > steady_score


def test_to_features_dict_excludes_identity_and_none():
    df = _windowed_df([100.0], [0.0])
    snapshot = compute_snapshot("BTCUSDT", "5m", df)
    features = snapshot.to_features_dict()
    for key in ("symbol", "period", "as_of", "data_points", "quality"):
        assert key not in features
    assert "long_liquidation_notional" in features
    assert "liquidation_notional_zscore" not in features  # was None


LIQUIDATION_MSG = {
    "e": "forceOrder", "E": 1700000000000,
    "o": {"s": "BTCUSDT", "S": "SELL", "o": "LIMIT", "f": "IOC", "q": "0.5",
          "p": "50000", "ap": "49950", "X": "FILLED", "l": "0.5", "z": "0.5", "T": 1700000000000},
}


class _FakeRepo:
    def __init__(self, windowed_df=None):
        self.inserted: list = []
        self._windowed_df = windowed_df if windowed_df is not None else pd.DataFrame()

    async def insert_events(self, events):
        self.inserted.extend(events)

    async def fetch_windowed_stats(self, symbol, window_seconds, num_buckets):
        return self._windowed_df


class _FakeFeatureRepo:
    def __init__(self):
        self.written = []

    async def upsert_snapshot(self, snapshot):
        self.written.append(snapshot)


class _Settings:
    symbols = ["BTCUSDT"]
    liquidation_window_seconds = 300
    liquidation_baseline_buckets = 20


def test_on_liquidation_message_buffers_configured_symbols_only():
    engine = LiquidationEngine(_FakeRepo(), _FakeFeatureRepo(), _Settings())

    engine.on_liquidation_message(LIQUIDATION_MSG)
    other_symbol_msg = {**LIQUIDATION_MSG, "o": {**LIQUIDATION_MSG["o"], "s": "DOGEUSDT"}}
    engine.on_liquidation_message(other_symbol_msg)

    assert len(engine._buffer) == 1
    assert engine._buffer[0].symbol == "BTCUSDT"


def test_on_liquidation_message_ignores_non_force_order_events():
    engine = LiquidationEngine(_FakeRepo(), _FakeFeatureRepo(), _Settings())
    engine.on_liquidation_message({"e": "someOtherEvent"})
    assert engine._buffer == []


@pytest.mark.asyncio
async def test_flush_buffer_writes_and_clears():
    repo = _FakeRepo()
    engine = LiquidationEngine(repo, _FakeFeatureRepo(), _Settings())
    engine.on_liquidation_message(LIQUIDATION_MSG)

    count = await engine.flush_buffer()

    assert count == 1
    assert len(repo.inserted) == 1
    assert engine._buffer == []


@pytest.mark.asyncio
async def test_run_snapshot_cycle_flushes_then_computes_per_symbol():
    windowed = _windowed_df([100.0] * 6, [0.0] * 6)
    repo = _FakeRepo(windowed)
    feature_repo = _FakeFeatureRepo()
    engine = LiquidationEngine(repo, feature_repo, _Settings())
    engine.on_liquidation_message(LIQUIDATION_MSG)

    results = await engine.run_snapshot_cycle()

    assert len(repo.inserted) == 1  # the buffered event got flushed first
    assert set(results.keys()) == {"BTCUSDT"}
    assert len(feature_repo.written) == 1
