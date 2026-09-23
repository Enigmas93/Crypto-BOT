import pytest

from aegis.collector.market_collector import MarketCollector
from aegis.config import Settings
from aegis.providers.binance.models import AggTrade, Kline, SymbolRules


class FakeRestClient:
    def __init__(self):
        self.kline_calls = []

    async def get_symbol_rules(self):
        return {
            "BTCUSDT": SymbolRules(
                symbol="BTCUSDT", status="TRADING", price_precision=2, quantity_precision=3,
                tick_size=0.1, step_size=0.001, min_notional=5.0,
            )
        }

    async def get_klines(self, symbol, interval, limit=500):
        self.kline_calls.append((symbol, interval, limit))
        row = [1000, "1", "2", "0.5", "1.5", "100", 1999, "150", 10, "60", "90", "0"]
        return [Kline.from_rest_row(symbol, interval, row)]

    async def aclose(self):
        pass


def _settings(**overrides) -> Settings:
    defaults = dict(collector_symbols="BTCUSDT", collector_intervals="1m", collector_kline_bootstrap_limit=5)
    defaults.update(overrides)
    return Settings(**defaults)


@pytest.mark.asyncio
async def test_bootstrap_fetches_every_symbol_interval_pair():
    events = []
    fake_rest = FakeRestClient()
    collector = MarketCollector(_settings(), on_event=events.append, rest_client=fake_rest)

    history = await collector.bootstrap()

    assert fake_rest.kline_calls == [("BTCUSDT", "1m", 5)]
    assert "BTCUSDT:1m" in history
    assert collector._dedup.last_event_time[("kline", "BTCUSDT:1m")] == 1999
    # bootstrapped candles are persisted too, not just used to seed dedup state
    assert len(events) == 1
    assert isinstance(events[0], Kline)
    assert events[0].close_time_ms == 1999


def test_build_streams_covers_kline_aggtrade_mark_book():
    collector = MarketCollector(_settings(), on_event=lambda e: None, rest_client=FakeRestClient())
    streams = collector.build_streams()
    assert "btcusdt@kline_1m" in streams
    assert "btcusdt@aggTrade" in streams
    assert "btcusdt@markPrice@1s" in streams
    assert "btcusdt@bookTicker" in streams


def _kline_payload(event_time_ms: int) -> dict:
    return {
        "e": "kline", "E": event_time_ms, "s": "BTCUSDT",
        "k": {"t": event_time_ms - 60000, "T": event_time_ms, "s": "BTCUSDT", "i": "1m",
              "f": 1, "L": 2, "o": "1", "c": "1.1", "h": "1.2", "l": "0.9",
              "v": "10", "n": 5, "x": False, "q": "11", "V": "5", "Q": "5.5", "B": "0"},
    }


def test_duplicate_kline_event_is_dropped():
    events = []
    collector = MarketCollector(_settings(), on_event=events.append, rest_client=FakeRestClient())

    collector._handle_message(_kline_payload(1000))
    collector._handle_message(_kline_payload(1000))  # exact repeat, e.g. after reconnect overlap
    collector._handle_message(_kline_payload(1001))

    assert len(events) == 2
    assert all(isinstance(e, Kline) for e in events)


def test_duplicate_agg_trade_is_dropped_by_id():
    events = []
    collector = MarketCollector(_settings(), on_event=events.append, rest_client=FakeRestClient())
    payload = {"e": "aggTrade", "E": 1, "s": "BTCUSDT", "a": 42, "p": "1", "q": "1",
               "f": 1, "l": 1, "T": 1, "m": False}

    collector._handle_message(payload)
    collector._handle_message(payload)  # same aggTradeId again
    payload_next = {**payload, "a": 43}
    collector._handle_message(payload_next)

    assert len(events) == 2
    assert all(isinstance(e, AggTrade) for e in events)


def test_malformed_message_is_logged_not_raised():
    collector = MarketCollector(_settings(), on_event=lambda e: (_ for _ in ()).throw(AssertionError("should not fire")),
                                 rest_client=FakeRestClient())
    # missing required "k" key for a kline event must not raise
    collector._handle_message({"e": "kline", "E": 1, "s": "BTCUSDT"})
