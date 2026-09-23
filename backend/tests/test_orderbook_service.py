from datetime import UTC, datetime

import pytest

from aegis.orderbook.service import OrderBookEngine, compute_snapshot

DEPTH_MSG = {
    "e": "depthUpdate", "E": 1700000000000, "T": 1700000000000, "s": "BTCUSDT",
    "U": 1, "u": 2, "pu": 0,
    "b": [["49999.0", "1.5"], ["49998.0", "2.0"]],
    "a": [["50001.0", "1.0"], ["50002.0", "3.0"]],
}


def test_no_data_when_book_is_empty():
    snapshot = compute_snapshot("BTCUSDT", None, [], [])
    assert snapshot.quality == "NO_DATA"
    assert snapshot.best_bid is None


def test_compute_snapshot_populates_top_of_book_fields():
    bids = [(49999.0, 1.5), (49998.0, 2.0)]
    asks = [(50001.0, 1.0), (50002.0, 3.0)]
    as_of = datetime(2026, 1, 1, tzinfo=UTC)

    snapshot = compute_snapshot("BTCUSDT", as_of, bids, asks)

    assert snapshot.quality == "OK"
    assert snapshot.best_bid == pytest.approx(49999.0)
    assert snapshot.best_ask == pytest.approx(50001.0)
    assert snapshot.spread == pytest.approx(2.0)
    assert snapshot.data_points == 2
    assert snapshot.bid_depth_notional == pytest.approx(49999.0 * 1.5 + 49998.0 * 2.0)
    assert snapshot.ask_depth_notional == pytest.approx(50001.0 * 1.0 + 50002.0 * 3.0)


def test_spread_pct_matches_hand_computed_value():
    bids = [(100.0, 1.0)]
    asks = [(102.0, 1.0)]
    snapshot = compute_snapshot("BTCUSDT", None, bids, asks)
    # mid=101, spread=2 -> 2/101*100
    assert snapshot.spread_pct == pytest.approx(2 / 101 * 100)


def test_to_features_dict_excludes_identity_and_none():
    snapshot = compute_snapshot("BTCUSDT", datetime(2026, 1, 1, tzinfo=UTC), [(100.0, 1.0)], [(101.0, 1.0)])
    features = snapshot.to_features_dict()
    for key in ("symbol", "period", "as_of", "data_points", "quality"):
        assert key not in features
    assert "best_bid" in features


class _FakeFeatureRepo:
    def __init__(self):
        self.written = []

    async def upsert_snapshot(self, snapshot):
        self.written.append(snapshot)


class _Settings:
    symbols = ["BTCUSDT", "ETHUSDT"]
    orderbook_depth_levels = 20


def test_on_depth_message_updates_latest_book_per_symbol():
    engine = OrderBookEngine(_FakeFeatureRepo(), _Settings())
    engine.on_depth_message(DEPTH_MSG)
    assert "BTCUSDT" in engine._latest
    assert engine._latest["BTCUSDT"].bids[0] == (49999.0, 1.5)


def test_on_depth_message_ignores_malformed_payload():
    engine = OrderBookEngine(_FakeFeatureRepo(), _Settings())
    engine.on_depth_message({"s": "BTCUSDT"})  # missing "b"/"a"
    assert engine._latest == {}


def test_build_streams_uses_configured_depth_level():
    engine = OrderBookEngine(_FakeFeatureRepo(), _Settings())
    streams = engine.build_streams()
    assert streams == ["btcusdt@depth20@100ms", "ethusdt@depth20@100ms"]


@pytest.mark.asyncio
async def test_compute_and_store_snapshot_no_data_before_any_message():
    feature_repo = _FakeFeatureRepo()
    engine = OrderBookEngine(feature_repo, _Settings())

    snapshot = await engine.compute_and_store_snapshot("BTCUSDT")

    assert snapshot.quality == "NO_DATA"
    assert feature_repo.written == [snapshot]  # NO_DATA snapshots still go through upsert_snapshot,
    # which itself is responsible for skipping persistence (as_of is None)


@pytest.mark.asyncio
async def test_run_snapshot_cycle_covers_every_configured_symbol():
    feature_repo = _FakeFeatureRepo()
    engine = OrderBookEngine(feature_repo, _Settings())
    engine.on_depth_message(DEPTH_MSG)  # only BTCUSDT has a book

    results = await engine.run_snapshot_cycle()

    assert set(results.keys()) == {"BTCUSDT", "ETHUSDT"}
    assert results["BTCUSDT"].quality == "OK"
    assert results["ETHUSDT"].quality == "NO_DATA"
