import numpy as np
import pandas as pd
import pytest

from aegis.technical.service import (
    MIN_CANDLES_FOR_FULL_HISTORY,
    TechnicalAnalysisService,
    compute_snapshot,
)


def _ohlcv_df(n: int, start_price: float = 100.0, trend: float = 0.5) -> pd.DataFrame:
    closes = start_price + np.arange(n) * trend
    open_time = pd.date_range("2026-01-01", periods=n, freq="1min", tz="UTC")
    return pd.DataFrame({
        "open_time": open_time,
        "close_time": open_time + pd.Timedelta(minutes=1) - pd.Timedelta(milliseconds=1),
        "open": closes - 0.1,
        "high": closes + 0.5,
        "low": closes - 0.5,
        "close": closes,
        "volume": np.full(n, 10.0),
        "quote_volume": np.full(n, 1000.0),
        "trades": np.full(n, 5),
        "taker_buy_base_volume": np.full(n, 5.0),
        "taker_buy_quote_volume": np.full(n, 500.0),
        "is_closed": True,
    })


def test_compute_snapshot_on_empty_dataframe_is_no_data():
    snapshot = compute_snapshot("BTCUSDT", "1m", pd.DataFrame())
    assert snapshot.quality == "NO_DATA"
    assert snapshot.as_of is None
    assert snapshot.close is None


def test_compute_snapshot_partial_history_below_threshold():
    df = _ohlcv_df(30)
    snapshot = compute_snapshot("BTCUSDT", "1m", df)
    assert snapshot.quality == "PARTIAL_HISTORY"
    assert snapshot.data_points == 30
    # short-period indicators are populated even with limited history...
    assert snapshot.ema_20 is not None
    assert snapshot.rsi_14 is not None
    # ...but EMA200 genuinely cannot exist yet - must stay None, never guessed
    assert snapshot.ema_200 is None


def test_compute_snapshot_full_history_populates_everything():
    df = _ohlcv_df(MIN_CANDLES_FOR_FULL_HISTORY + 10, trend=1.0)
    snapshot = compute_snapshot("BTCUSDT", "1h", df)
    assert snapshot.quality == "OK"
    for field in (
        "ema_20", "ema_50", "ema_100", "ema_200", "sma_20", "rsi_14", "atr_14",
        "adx_14", "vwap", "bb_mid", "bb_upper", "bb_lower", "macd", "macd_signal",
        "roc_12", "momentum_10", "distance_from_ema200_pct",
    ):
        assert getattr(snapshot, field) is not None, f"{field} should be populated"
    assert snapshot.as_of == df["close_time"].iloc[-1].to_pydatetime()
    assert snapshot.market_structure_trend in {"UPTREND", "DOWNTREND", "RANGING", "UNKNOWN"}


def test_compute_snapshot_uptrend_has_positive_distance_from_ema200():
    df = _ohlcv_df(MIN_CANDLES_FOR_FULL_HISTORY + 10, trend=1.0)  # steadily rising
    snapshot = compute_snapshot("BTCUSDT", "1h", df)
    assert snapshot.distance_from_ema200_pct > 0  # price above a lagging EMA in an uptrend


def test_to_features_dict_excludes_identity_columns():
    df = _ohlcv_df(50)
    snapshot = compute_snapshot("ETHUSDT", "5m", df)
    features = snapshot.to_features_dict()
    assert "symbol" not in features
    assert "interval" not in features
    assert "as_of" not in features
    assert "data_points" not in features
    assert "quality" not in features  # already its own column in market_features
    assert "close" in features


class _FakeCandleSource:
    def __init__(self, frames: dict[tuple[str, str], pd.DataFrame]):
        self._frames = frames
        self.calls: list[tuple[str, str, int]] = []

    async def fetch_ohlcv(self, symbol, interval, limit=500, closed_only=True):
        self.calls.append((symbol, interval, limit))
        return self._frames.get((symbol, interval), pd.DataFrame())


@pytest.mark.asyncio
async def test_service_computes_snapshot_per_configured_interval():
    frames = {
        ("BTCUSDT", "1m"): _ohlcv_df(60),
        ("BTCUSDT", "1h"): _ohlcv_df(40),
    }
    source = _FakeCandleSource(frames)
    service = TechnicalAnalysisService(candle_source=source, intervals=["1m", "1h"])

    result = await service.compute_for_symbol("BTCUSDT")

    assert set(result.keys()) == {"1m", "1h"}
    assert result["1m"].data_points == 60
    assert result["1h"].data_points == 40
    assert source.calls == [("BTCUSDT", "1m", 500), ("BTCUSDT", "1h", 500)]


@pytest.mark.asyncio
async def test_service_handles_symbol_with_no_history_gracefully():
    source = _FakeCandleSource({})
    service = TechnicalAnalysisService(candle_source=source, intervals=["1m"])

    result = await service.compute_for_symbol("BRAND_NEW_LISTING")

    assert result["1m"].quality == "NO_DATA"
