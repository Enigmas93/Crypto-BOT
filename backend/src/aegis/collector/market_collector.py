"""MarketCollector - Phase 1 orchestrator.

Wires the REST client (bootstrap + reconciliation) and the WebSocket client
(live streaming) together, and turns raw Binance payloads into the typed
models in `aegis.providers.binance.models`. This is the first concrete
implementation of the `MarketDataProvider` interface.

Persistence (Phase 2) is intentionally not here: `on_event` is the seam
where a database writer, a Redis publisher, or - for now - a log line gets
attached without this module knowing or caring which.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from aegis.config import Settings
from aegis.logging_utils import get_logger, log_event
from aegis.providers.binance.models import AggTrade, BookTicker, Kline, MarkPrice
from aegis.providers.binance.rest_client import BinanceFuturesRestClient
from aegis.providers.binance.ws_client import BinanceFuturesWebSocketClient

_LOG = get_logger("collector.market")

MarketEvent = Kline | AggTrade | MarkPrice | BookTicker
EventHandler = Callable[[MarketEvent], None]


@dataclass
class _DedupState:
    last_event_time: dict[tuple[str, str], int] = field(default_factory=dict)
    last_agg_trade_id: dict[str, int] = field(default_factory=dict)

    def kline_is_new(self, symbol: str, interval: str, event_time_ms: int) -> bool:
        key = ("kline", f"{symbol}:{interval}")
        last = self.last_event_time.get(key, -1)
        if event_time_ms <= last:
            return False
        self.last_event_time[key] = event_time_ms
        return True

    def tagged_is_new(self, tag: str, symbol: str, event_time_ms: int) -> bool:
        key = (tag, symbol)
        last = self.last_event_time.get(key, -1)
        if event_time_ms <= last:
            return False
        self.last_event_time[key] = event_time_ms
        return True

    def agg_trade_is_new(self, symbol: str, agg_trade_id: int) -> bool:
        last = self.last_agg_trade_id.get(symbol, -1)
        if agg_trade_id <= last:
            return False
        self.last_agg_trade_id[symbol] = agg_trade_id
        return True


class MarketCollector:
    def __init__(
        self,
        settings: Settings,
        on_event: EventHandler,
        rest_client: BinanceFuturesRestClient | None = None,
        gap_fill_limit: int = 10,
    ) -> None:
        self.settings = settings
        self.on_event = on_event
        self.rest = rest_client or BinanceFuturesRestClient(testnet=settings.binance_testnet)
        self.gap_fill_limit = gap_fill_limit
        self._dedup = _DedupState()
        self._ws: BinanceFuturesWebSocketClient | None = None

    def build_streams(self) -> list[str]:
        streams: list[str] = []
        for symbol in self.settings.symbols:
            s = symbol.lower()
            for interval in self.settings.intervals:
                streams.append(f"{s}@kline_{interval}")
            streams.append(f"{s}@aggTrade")
            streams.append(f"{s}@markPrice@1s")
            streams.append(f"{s}@bookTicker")
        return streams

    async def bootstrap(self) -> dict[str, list[Kline]]:
        """REST bootstrap: exchange rules + historical klines for every
        configured (symbol, interval), before the WS stream even opens.

        Every bootstrapped candle is also forwarded through `on_event`, the
        same as a live one - otherwise persistence only ever sees candles
        streamed after the process started, and indicators that need real
        depth (EMA200 needs 200+ closed bars) would never have enough
        history to compute for days after every fresh start."""
        rules = await self.rest.get_symbol_rules()
        missing = [s for s in self.settings.symbols if s not in rules]
        if missing:
            log_event(_LOG, "bootstrap_unknown_symbols", level=30, symbols=missing)

        history: dict[str, list[Kline]] = {}
        for symbol in self.settings.symbols:
            for interval in self.settings.intervals:
                klines = await self.rest.get_klines(
                    symbol, interval, limit=self.settings.collector_kline_bootstrap_limit
                )
                history[f"{symbol}:{interval}"] = klines
                for k in klines:
                    if self._dedup.kline_is_new(symbol, interval, k.close_time_ms):
                        self.on_event(k)
        log_event(
            _LOG, "bootstrap_complete",
            symbols=len(self.settings.symbols), series=len(history),
        )
        return history

    async def _gap_fill(self) -> None:
        """Re-fetch the most recent klines over REST after a reconnect, so a
        gap in the WebSocket stream never becomes a silent gap in the data
        (spec section 6/108: reconcile before trusting the stream again)."""
        for symbol in self.settings.symbols:
            for interval in self.settings.intervals:
                try:
                    klines = await self.rest.get_klines(symbol, interval, limit=self.gap_fill_limit)
                except Exception as exc:  # noqa: BLE001
                    log_event(
                        _LOG, "gap_fill_failed", level=40,
                        symbol=symbol, interval=interval, error=str(exc),
                    )
                    continue
                for k in klines:
                    if self._dedup.kline_is_new(symbol, interval, k.close_time_ms):
                        self.on_event(k)
        log_event(_LOG, "gap_fill_complete")

    def _handle_message(self, data: dict[str, Any]) -> None:
        event_type = data.get("e")
        try:
            if event_type == "kline":
                k = Kline.from_ws_payload(data)
                if self._dedup.kline_is_new(k.symbol, k.interval, int(data["E"])):
                    self.on_event(k)
            elif event_type == "aggTrade":
                t = AggTrade.from_ws_payload(data)
                if self._dedup.agg_trade_is_new(t.symbol, t.agg_trade_id):
                    self.on_event(t)
            elif event_type == "markPriceUpdate":
                m = MarkPrice.from_ws_payload(data)
                if self._dedup.tagged_is_new("mark", m.symbol, m.event_time_ms):
                    self.on_event(m)
            elif event_type == "bookTicker":
                b = BookTicker.from_ws_payload(data)
                # bookTicker updates can share a timestamp across rapid ticks;
                # allow equal timestamps through, only reject strictly stale ones.
                key = ("book", b.symbol)
                if b.event_time_ms >= self._dedup.last_event_time.get(key, -1):
                    self._dedup.last_event_time[key] = b.event_time_ms
                    self.on_event(b)
            else:
                log_event(_LOG, "ws_unknown_event_type", level=20, event_type=str(event_type))
        except (KeyError, ValueError, TypeError) as exc:
            log_event(_LOG, "ws_payload_parse_error", level=40, error=str(exc), event_type=str(event_type))

    async def run(self) -> None:
        await self.bootstrap()
        streams = self.build_streams()
        self._ws = BinanceFuturesWebSocketClient(streams=streams, testnet=self.settings.binance_testnet)
        log_event(_LOG, "collector_starting", stream_count=len(streams))
        await self._ws.run(on_message=self._handle_message, on_reconnect=self._gap_fill)

    def stop(self) -> None:
        if self._ws is not None:
            self._ws.stop()

    async def aclose(self) -> None:
        await self.rest.aclose()
