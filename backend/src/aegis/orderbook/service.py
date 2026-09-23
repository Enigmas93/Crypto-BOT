"""OrderBookEngine - spec section 30.

Ingests Binance's partial book depth stream (`<symbol>@depth<levels>@100ms`)
- a full top-N snapshot on every push, so no local diff-based book
reconstruction or sequence-gap handling is needed, unlike the raw `@depth`
stream. Only the *latest* book per symbol is kept in memory; periodically a
derived feature snapshot is computed and merged into `market_features`.

Deliberately not persisted: the raw depth ticks themselves. Phase 2 already
learned that lesson with `book_ticker` (high-frequency top-of-book updates
growing the table fast with limited research value) - full L2 snapshots at
10/s would be worse. If historical order-book replay is ever needed for
backtesting, that is a deliberate future addition, not an oversight.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from aegis.logging_utils import get_logger, log_event
from aegis.orderbook import analytics
from aegis.providers.binance.models import PartialDepthUpdate
from aegis.providers.binance.ws_client import BinanceFuturesWebSocketClient

_LOG = get_logger("orderbook.service")

ORDERBOOK_PERIOD = "orderbook"  # constant label - a point-in-time snapshot, not a candle-interval bucket


@dataclass(slots=True)
class OrderBookFeatureSnapshot:
    symbol: str
    as_of: datetime | None
    data_points: int
    quality: str  # NO_DATA | OK
    period: str = ORDERBOOK_PERIOD

    best_bid: float | None = None
    best_ask: float | None = None
    spread: float | None = None
    spread_pct: float | None = None
    microprice: float | None = None
    top_of_book_imbalance: float | None = None
    depth_imbalance: float | None = None
    book_pressure: float | None = None
    bid_depth_notional: float | None = None
    ask_depth_notional: float | None = None

    def to_features_dict(self) -> dict:
        d = asdict(self)
        for key in ("symbol", "period", "as_of", "data_points", "quality"):
            d.pop(key, None)
        return {k: v for k, v in d.items() if v is not None}


def compute_snapshot(symbol: str, as_of: datetime | None, bids: list, asks: list) -> OrderBookFeatureSnapshot:
    if not bids or not asks:
        return OrderBookFeatureSnapshot(symbol=symbol, as_of=None, data_points=0, quality="NO_DATA")

    best_bid_price, best_bid_qty = bids[0]
    best_ask_price, best_ask_qty = asks[0]
    mid = (best_bid_price + best_ask_price) / 2
    spread = best_ask_price - best_bid_price
    spread_pct = (spread / mid * 100) if mid else None

    bid_notional = analytics.depth_notional(bids)
    ask_notional = analytics.depth_notional(asks)

    return OrderBookFeatureSnapshot(
        symbol=symbol,
        as_of=as_of,
        data_points=min(len(bids), len(asks)),
        quality="OK",
        best_bid=best_bid_price,
        best_ask=best_ask_price,
        spread=spread,
        spread_pct=spread_pct,
        microprice=analytics.microprice(best_bid_price, best_bid_qty, best_ask_price, best_ask_qty),
        top_of_book_imbalance=analytics.top_of_book_imbalance(best_bid_qty, best_ask_qty),
        depth_imbalance=analytics.depth_imbalance(bid_notional, ask_notional),
        book_pressure=analytics.book_pressure(bids, asks),
        bid_depth_notional=bid_notional,
        ask_depth_notional=ask_notional,
    )


class OrderBookEngine:
    def __init__(self, feature_repo, settings) -> None:
        self.feature_repo = feature_repo
        self.settings = settings
        self._latest: dict[str, PartialDepthUpdate] = {}
        self._ws: BinanceFuturesWebSocketClient | None = None

    def on_depth_message(self, data: dict) -> None:
        try:
            update = PartialDepthUpdate.from_ws_payload(data)
        except (KeyError, ValueError, TypeError) as exc:
            log_event(_LOG, "orderbook_parse_error", level=40, error=str(exc))
            return
        self._latest[update.symbol] = update

    async def compute_and_store_snapshot(self, symbol: str) -> OrderBookFeatureSnapshot:
        update = self._latest.get(symbol)
        if update is None:
            snapshot = OrderBookFeatureSnapshot(symbol=symbol, as_of=None, data_points=0, quality="NO_DATA")
        else:
            as_of = datetime.fromtimestamp(update.event_time_ms / 1000.0, tz=UTC)
            snapshot = compute_snapshot(symbol, as_of, update.bids, update.asks)
        await self.feature_repo.upsert_snapshot(snapshot)
        return snapshot

    async def run_snapshot_cycle(self) -> dict[str, OrderBookFeatureSnapshot]:
        results: dict[str, OrderBookFeatureSnapshot] = {}
        for symbol in self.settings.symbols:
            try:
                results[symbol] = await self.compute_and_store_snapshot(symbol)
            except Exception as exc:  # noqa: BLE001 - one bad symbol must not skip the rest
                log_event(_LOG, "orderbook_snapshot_failed", level=40, symbol=symbol, error=str(exc))
        return results

    async def _snapshot_loop(self) -> None:
        while True:
            await asyncio.sleep(self.settings.orderbook_snapshot_interval_seconds)
            results = await self.run_snapshot_cycle()
            for symbol, snapshot in results.items():
                log_event(_LOG, "orderbook_snapshot", symbol=symbol, quality=snapshot.quality,
                           spread=snapshot.spread, book_pressure=snapshot.book_pressure)

    def build_streams(self) -> list[str]:
        levels = self.settings.orderbook_depth_levels
        return [f"{s.lower()}@depth{levels}@100ms" for s in self.settings.symbols]

    async def run(self) -> None:
        self._ws = BinanceFuturesWebSocketClient(streams=self.build_streams(), testnet=self.settings.binance_testnet)
        snapshot_task = asyncio.create_task(self._snapshot_loop())
        try:
            await self._ws.run(on_message=self.on_depth_message)
        finally:
            snapshot_task.cancel()

    def stop(self) -> None:
        if self._ws is not None:
            self._ws.stop()
