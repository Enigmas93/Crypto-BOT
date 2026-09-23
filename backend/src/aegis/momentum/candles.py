"""REST->DataFrame adapter for the Momentum Engine (Fase 14).

Unlike every other engine, Momentum doesn't read from the collector's
persisted `candles` table - its symbol universe is chosen dynamically by
the Scanner, and pre-subscribing the collector's WebSocket to a rotating
set of symbols would be a much bigger architecture change than fetching
recent klines directly over REST each cycle (`get_klines`, already public
and unauthenticated). The one thing that direct REST path does NOT do for
you, that the collector's WS pipeline normally guarantees, is excluding
the still-forming candle - Binance's klines endpoint includes the current,
incomplete bar as the last row if you don't bound the query by an end
time. Treating that bar as "closed" would be a real lookahead bug (a
decision partly based on data from a bar that hasn't finished happening
yet) - `klines_to_closed_dataframe` exists specifically to prevent that.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pandas as pd

from aegis.providers.binance.models import Kline

_COLUMNS = [
    "open_time", "close_time", "open", "high", "low", "close", "volume",
    "quote_volume", "trades", "taker_buy_base_volume", "taker_buy_quote_volume", "is_closed",
]


def klines_to_closed_dataframe(klines: list[Kline], now: datetime | None = None) -> pd.DataFrame:
    """Converts REST klines into the same shape CandleRepository.fetch_ohlcv
    returns (ascending by open_time), dropping any bar whose close_time is
    still in the future - the currently-forming candle, if present."""
    now = now or datetime.now(UTC)
    closed = [k for k in klines if datetime.fromtimestamp(k.close_time_ms / 1000, tz=UTC) <= now]
    if not closed:
        return pd.DataFrame(columns=_COLUMNS)

    return pd.DataFrame({
        "open_time": [pd.Timestamp(k.open_time_ms, unit="ms", tz="UTC") for k in closed],
        "close_time": [pd.Timestamp(k.close_time_ms, unit="ms", tz="UTC") for k in closed],
        "open": [k.open for k in closed],
        "high": [k.high for k in closed],
        "low": [k.low for k in closed],
        "close": [k.close for k in closed],
        "volume": [k.volume for k in closed],
        "quote_volume": [k.quote_volume for k in closed],
        "trades": [k.trades for k in closed],
        "taker_buy_base_volume": [k.taker_buy_base_volume for k in closed],
        "taker_buy_quote_volume": [k.taker_buy_quote_volume for k in closed],
        "is_closed": [True] * len(closed),
    })
