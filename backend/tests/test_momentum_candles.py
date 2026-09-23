from datetime import UTC, datetime

from aegis.momentum.candles import klines_to_closed_dataframe
from aegis.providers.binance.models import Kline


def _kline(open_time_ms: int, close_time_ms: int, close: float = 100.0) -> Kline:
    return Kline(
        symbol="BTCUSDT", interval="15m", open_time_ms=open_time_ms, close_time_ms=close_time_ms,
        open=close, high=close + 1, low=close - 1, close=close, volume=10.0, quote_volume=1000.0,
        trades=5, taker_buy_base_volume=5.0, taker_buy_quote_volume=500.0, is_closed=True,
    )


def test_drops_the_still_forming_last_candle():
    now = datetime(2026, 1, 1, 12, 7, 0, tzinfo=UTC)  # mid-way through a 15m bar
    # build two clean, unambiguous bars: one that closed in the past, one still forming
    past_open = int(datetime(2026, 1, 1, 11, 45, tzinfo=UTC).timestamp() * 1000)
    past_close = int(datetime(2026, 1, 1, 11, 59, 59, 999000, tzinfo=UTC).timestamp() * 1000)
    forming_open = int(datetime(2026, 1, 1, 12, 0, tzinfo=UTC).timestamp() * 1000)
    forming_close = int(datetime(2026, 1, 1, 12, 14, 59, 999000, tzinfo=UTC).timestamp() * 1000)
    klines = [_kline(past_open, past_close, close=100.0), _kline(forming_open, forming_close, close=105.0)]

    df = klines_to_closed_dataframe(klines, now=now)

    assert len(df) == 1
    assert df["close"].iloc[0] == 100.0


def test_keeps_a_bar_that_closed_exactly_at_now():
    now = datetime(2026, 1, 1, 11, 59, 59, 999000, tzinfo=UTC)
    past_open = int(datetime(2026, 1, 1, 11, 45, tzinfo=UTC).timestamp() * 1000)
    past_close = int(now.timestamp() * 1000)
    df = klines_to_closed_dataframe([_kline(past_open, past_close)], now=now)
    assert len(df) == 1


def test_empty_input_gives_empty_dataframe_with_expected_columns():
    df = klines_to_closed_dataframe([], now=datetime(2026, 1, 1, tzinfo=UTC))
    assert df.empty
    assert list(df.columns) == [
        "open_time", "close_time", "open", "high", "low", "close", "volume",
        "quote_volume", "trades", "taker_buy_base_volume", "taker_buy_quote_volume", "is_closed",
    ]


def test_all_forming_gives_empty_dataframe():
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    forming_open = int(datetime(2026, 1, 1, 12, 0, tzinfo=UTC).timestamp() * 1000)
    forming_close = int(datetime(2026, 1, 1, 12, 14, 59, 999000, tzinfo=UTC).timestamp() * 1000)
    df = klines_to_closed_dataframe([_kline(forming_open, forming_close)], now=now)
    assert df.empty


def test_preserves_ascending_order_and_all_ohlcv_fields():
    now = datetime(2026, 1, 1, 13, 0, tzinfo=UTC)
    bar1 = _kline(
        int(datetime(2026, 1, 1, 11, 0, tzinfo=UTC).timestamp() * 1000),
        int(datetime(2026, 1, 1, 11, 14, 59, 999000, tzinfo=UTC).timestamp() * 1000),
        close=100.0,
    )
    bar2 = _kline(
        int(datetime(2026, 1, 1, 11, 15, tzinfo=UTC).timestamp() * 1000),
        int(datetime(2026, 1, 1, 11, 29, 59, 999000, tzinfo=UTC).timestamp() * 1000),
        close=101.0,
    )
    df = klines_to_closed_dataframe([bar1, bar2], now=now)
    assert list(df["close"]) == [100.0, 101.0]
    assert df["is_closed"].all()
