from aegis.providers.binance.models import AggTrade, BookTicker, Kline, MarkPrice, OpenInterest

REST_KLINE_ROW = [
    1499040000000, "0.01634790", "0.80000000", "0.01575800", "0.01577100",
    "148976.11427815", 1499644799999, "2434.19055334", 308,
    "1756.87402397", "28.46694368", "17928899.62484339",
]

WS_KLINE_PAYLOAD = {
    "e": "kline", "E": 1638747660000, "s": "BTCUSDT",
    "k": {
        "t": 1638747660000, "T": 1638747719999, "s": "BTCUSDT", "i": "1m",
        "f": 100, "L": 200, "o": "0.0010", "c": "0.0020", "h": "0.0025", "l": "0.0015",
        "v": "1000", "n": 100, "x": False, "q": "1.0000", "V": "500", "Q": "0.500", "B": "123456",
    },
}


def test_kline_from_rest_row():
    k = Kline.from_rest_row("BTCUSDT", "1m", REST_KLINE_ROW)
    assert k.symbol == "BTCUSDT"
    assert k.open == 0.01634790
    assert k.close == 0.01577100
    assert k.trades == 308
    assert k.is_closed is True  # close_time_ms (1499644799999) is genuinely in the past


def test_kline_from_rest_row_still_forming_candle_is_not_closed():
    """Binance's REST kline endpoint always includes the current,
    still-forming candle as the last row when queried without an endTime -
    `is_closed` must reflect that (regression: used to be hardcoded True,
    which let a bootstrap/gap-fill persist an open bar as if it were
    final - found live for the daily interval, which stays open ~24h)."""
    close_time_ms = REST_KLINE_ROW[6]
    k = Kline.from_rest_row("BTCUSDT", "1d", REST_KLINE_ROW, now_ms=close_time_ms - 1)
    assert k.is_closed is False


def test_kline_from_rest_row_closed_candle_stays_closed():
    close_time_ms = REST_KLINE_ROW[6]
    k = Kline.from_rest_row("BTCUSDT", "1d", REST_KLINE_ROW, now_ms=close_time_ms + 1)
    assert k.is_closed is True


def test_kline_from_ws_payload_open_candle_not_closed():
    k = Kline.from_ws_payload(WS_KLINE_PAYLOAD)
    assert k.symbol == "BTCUSDT"
    assert k.interval == "1m"
    assert k.is_closed is False
    assert k.open == 0.0010


def test_agg_trade_from_ws_payload():
    payload = {"e": "aggTrade", "E": 1, "s": "BTCUSDT", "a": 5933014, "p": "0.001",
               "q": "100", "f": 100, "l": 105, "T": 123456785, "m": True}
    t = AggTrade.from_ws_payload(payload)
    assert t.agg_trade_id == 5933014
    assert t.is_buyer_maker is True


def test_mark_price_from_ws_payload():
    payload = {"e": "markPriceUpdate", "E": 1562305380000, "s": "BTCUSDT",
               "p": "11185.87786614", "i": "11784.62659091", "P": "11784.25641265",
               "r": "0.00030000", "T": 1562306400000}
    m = MarkPrice.from_ws_payload(payload)
    assert m.funding_rate == 0.00030000
    assert m.mark_price == 11185.87786614


def test_book_ticker_from_ws_payload():
    payload = {"e": "bookTicker", "u": 400900217, "s": "BNBUSDT", "b": "25.35190000",
               "B": "31.21000000", "a": "25.36520000", "A": "40.66000000",
               "T": 123456789, "E": 123456788}
    b = BookTicker.from_ws_payload(payload)
    assert b.best_bid_price == 25.35190000
    assert b.best_ask_price == 25.36520000


def test_open_interest_from_rest_payload():
    payload = {"symbol": "BTCUSDT", "openInterest": "10659.509", "time": 1589437530011}
    oi = OpenInterest.from_rest_payload(payload)
    assert oi.open_interest == 10659.509
