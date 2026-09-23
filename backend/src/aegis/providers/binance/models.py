"""Typed representations of Binance Futures market data.

Every value carries enough metadata (source, quality) to satisfy the
DataQuality principle (spec section 44) once persistence lands in Phase 2.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class Kline:
    symbol: str
    interval: str
    open_time_ms: int
    close_time_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    quote_volume: float
    trades: int
    taker_buy_base_volume: float
    taker_buy_quote_volume: float
    is_closed: bool
    source: str = "binance"

    @classmethod
    def from_rest_row(cls, symbol: str, interval: str, row: list[Any], now_ms: int | None = None) -> "Kline":
        """Binance's kline REST endpoint always includes the still-forming
        current candle as the last row when queried without an explicit
        endTime (which every caller here does, for the "latest N candles"
        use case) - so `is_closed` can NOT be hardcoded True, or a
        genuinely open bar gets persisted/traded on as if it were final
        (found live: the collector's REST bootstrap/gap-fill was doing
        exactly this for every symbol's daily candle, which stays "open"
        for ~24h - long enough to be visibly wrong in the dashboard's data
        health check, unlike shorter intervals where a real close arrives
        via WebSocket within minutes and quietly overwrites the bad row).
        `now_ms` is only for tests; real callers use wall-clock time."""
        close_time_ms = int(row[6])
        if now_ms is None:
            now_ms = int(time.time() * 1000)
        return cls(
            symbol=symbol,
            interval=interval,
            open_time_ms=int(row[0]),
            close_time_ms=close_time_ms,
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
            volume=float(row[5]),
            quote_volume=float(row[7]),
            trades=int(row[8]),
            taker_buy_base_volume=float(row[9]),
            taker_buy_quote_volume=float(row[10]),
            is_closed=close_time_ms < now_ms,
        )

    @classmethod
    def from_ws_payload(cls, payload: dict[str, Any]) -> "Kline":
        k = payload["k"]
        return cls(
            symbol=k["s"],
            interval=k["i"],
            open_time_ms=int(k["t"]),
            close_time_ms=int(k["T"]),
            open=float(k["o"]),
            high=float(k["h"]),
            low=float(k["l"]),
            close=float(k["c"]),
            volume=float(k["v"]),
            quote_volume=float(k["q"]),
            trades=int(k["n"]),
            taker_buy_base_volume=float(k["V"]),
            taker_buy_quote_volume=float(k["Q"]),
            is_closed=bool(k["x"]),
        )


@dataclass(slots=True)
class AggTrade:
    symbol: str
    agg_trade_id: int
    price: float
    quantity: float
    trade_time_ms: int
    is_buyer_maker: bool  # True => sell-side aggressor (taker sold into a bid)
    source: str = "binance"

    @classmethod
    def from_ws_payload(cls, payload: dict[str, Any]) -> "AggTrade":
        return cls(
            symbol=payload["s"],
            agg_trade_id=int(payload["a"]),
            price=float(payload["p"]),
            quantity=float(payload["q"]),
            trade_time_ms=int(payload["T"]),
            is_buyer_maker=bool(payload["m"]),
        )


@dataclass(slots=True)
class MarkPrice:
    symbol: str
    mark_price: float
    index_price: float
    estimated_settle_price: float
    funding_rate: float
    next_funding_time_ms: int
    event_time_ms: int
    source: str = "binance"

    @classmethod
    def from_ws_payload(cls, payload: dict[str, Any]) -> "MarkPrice":
        return cls(
            symbol=payload["s"],
            mark_price=float(payload["p"]),
            index_price=float(payload["i"]),
            estimated_settle_price=float(payload.get("P", 0.0) or 0.0),
            funding_rate=float(payload["r"]),
            next_funding_time_ms=int(payload["T"]),
            event_time_ms=int(payload["E"]),
        )


@dataclass(slots=True)
class BookTicker:
    symbol: str
    best_bid_price: float
    best_bid_qty: float
    best_ask_price: float
    best_ask_qty: float
    event_time_ms: int
    source: str = "binance"

    @classmethod
    def from_ws_payload(cls, payload: dict[str, Any]) -> "BookTicker":
        return cls(
            symbol=payload["s"],
            best_bid_price=float(payload["b"]),
            best_bid_qty=float(payload["B"]),
            best_ask_price=float(payload["a"]),
            best_ask_qty=float(payload["A"]),
            event_time_ms=int(payload.get("E", 0) or 0),
        )


@dataclass(slots=True)
class OpenInterest:
    symbol: str
    open_interest: float
    timestamp_ms: int
    source: str = "binance"

    @classmethod
    def from_rest_payload(cls, payload: dict[str, Any]) -> "OpenInterest":
        return cls(
            symbol=payload["symbol"],
            open_interest=float(payload["openInterest"]),
            timestamp_ms=int(payload["time"]),
        )


@dataclass(slots=True)
class OpenInterestHistPoint:
    symbol: str
    period: str
    sum_open_interest: float
    sum_open_interest_value: float
    timestamp_ms: int
    source: str = "binance"

    @classmethod
    def from_rest_payload(cls, symbol: str, period: str, payload: dict[str, Any]) -> "OpenInterestHistPoint":
        return cls(
            symbol=symbol,
            period=period,
            sum_open_interest=float(payload["sumOpenInterest"]),
            sum_open_interest_value=float(payload["sumOpenInterestValue"]),
            timestamp_ms=int(payload["timestamp"]),
        )


@dataclass(slots=True)
class LongShortRatioPoint:
    symbol: str
    ratio_type: str  # GLOBAL_ACCOUNT | TOP_ACCOUNT | TOP_POSITION
    period: str
    long_short_ratio: float
    long_account: float
    short_account: float
    timestamp_ms: int
    source: str = "binance"

    @classmethod
    def from_rest_payload(
        cls, symbol: str, ratio_type: str, period: str, payload: dict[str, Any]
    ) -> "LongShortRatioPoint":
        return cls(
            symbol=symbol,
            ratio_type=ratio_type,
            period=period,
            long_short_ratio=float(payload["longShortRatio"]),
            long_account=float(payload["longAccount"]),
            short_account=float(payload["shortAccount"]),
            timestamp_ms=int(payload["timestamp"]),
        )


@dataclass(slots=True)
class LiquidationEvent:
    symbol: str
    side: str  # SELL = a long position was force-liquidated, BUY = a short was
    quantity: float
    price: float
    average_price: float
    order_status: str
    event_time_ms: int
    source: str = "binance"

    @property
    def notional(self) -> float:
        return self.quantity * self.average_price

    @classmethod
    def from_ws_payload(cls, payload: dict[str, Any]) -> "LiquidationEvent":
        order = payload["o"]
        return cls(
            symbol=order["s"],
            side=order["S"],
            quantity=float(order["q"]),
            price=float(order["p"]),
            average_price=float(order["ap"]),
            order_status=order["X"],
            event_time_ms=int(order["T"]),
        )


@dataclass(slots=True)
class PartialDepthUpdate:
    """Top-N bid/ask levels from Binance's partial book depth stream - a
    full snapshot on every push, not a diff, so no local book
    reconstruction or sequence-gap handling is needed (unlike the raw
    `@depth` diff stream)."""

    symbol: str
    event_time_ms: int
    bids: list[tuple[float, float]]  # (price, quantity), best bid first
    asks: list[tuple[float, float]]  # (price, quantity), best ask first
    source: str = "binance"

    @classmethod
    def from_ws_payload(cls, payload: dict[str, Any]) -> "PartialDepthUpdate":
        return cls(
            symbol=payload["s"],
            event_time_ms=int(payload.get("T") or payload["E"]),
            bids=[(float(p), float(q)) for p, q in payload["b"]],
            asks=[(float(p), float(q)) for p, q in payload["a"]],
        )


@dataclass(slots=True)
class SymbolRules:
    symbol: str
    status: str
    price_precision: int
    quantity_precision: int
    tick_size: float
    step_size: float
    min_notional: float
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass(slots=True)
class OrderResult:
    """Response from a signed order endpoint (place/cancel/query) - spec
    section 120. Binance's own field names are abbreviated/camelCase; this
    normalizes them into the same style as the rest of this module."""
    order_id: int
    client_order_id: str
    symbol: str
    side: str  # BUY | SELL
    type: str  # MARKET | STOP_MARKET | TAKE_PROFIT_MARKET | ...
    status: str  # NEW | FILLED | PARTIALLY_FILLED | CANCELED | EXPIRED | REJECTED
    quantity: float
    executed_qty: float
    avg_price: float
    reduce_only: bool
    close_position: bool
    stop_price: float | None
    update_time_ms: int
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_rest_payload(cls, payload: dict[str, Any]) -> "OrderResult":
        return cls(
            order_id=int(payload["orderId"]),
            client_order_id=str(payload.get("clientOrderId", "")),
            symbol=payload["symbol"],
            side=payload["side"],
            type=payload["type"],
            status=payload["status"],
            quantity=float(payload.get("origQty", 0) or 0),
            executed_qty=float(payload.get("executedQty", 0) or 0),
            avg_price=float(payload.get("avgPrice", 0) or 0),
            reduce_only=bool(payload.get("reduceOnly", False)),
            close_position=bool(payload.get("closePosition", False)),
            stop_price=float(payload["stopPrice"]) if payload.get("stopPrice") not in (None, "0", "") else None,
            update_time_ms=int(payload.get("updateTime", 0) or 0),
            raw=payload,
        )


_ALGO_STATUS_MAP = {"FINISHED": "FILLED", "NEW": "NEW", "CANCELED": "CANCELED", "EXPIRED": "EXPIRED"}


@dataclass(slots=True)
class AlgoOrderResult:
    """Response from the Algo Order endpoint (/fapi/v1/algoOrder) - the
    dedicated endpoint STOP_MARKET/TAKE_PROFIT_MARKET/STOP/TAKE_PROFIT/
    TRAILING_STOP_MARKET orders moved to on 2025-12-09 (the old
    /fapi/v1/order endpoint now rejects them with error -4120). Binance's
    field names differ from a plain order's (`algoId` not `orderId`,
    `algoStatus` not `status`, `actualPrice`/`actualQty` not `avgPrice`/
    `executedQty`, `triggerPrice` not `stopPrice`) - normalized here to
    the SAME attribute names `OrderResult` uses, so calling code
    (BinanceExecutionProvider, ShadowTradingEngine) can treat the two
    interchangeably without caring which endpoint actually served a given
    order.

    `algoStatus` values (confirmed live + via docs): NEW (not yet
    triggered), FINISHED (triggered and executed - mapped to FILLED here),
    CANCELED, EXPIRED. `actualPrice`/`actualQty` are 0/absent until the
    order actually triggers - mapped to 0.0 here (never a fabricated
    value) rather than left as None, since a numeric 0 is meaningful here
    (no fill yet), unlike NULL-ing out the whole field."""
    order_id: int  # Binance's algoId
    client_order_id: str  # Binance's clientAlgoId
    symbol: str
    side: str
    type: str  # Binance's orderType
    status: str  # normalized from algoStatus - see docstring
    quantity: float
    executed_qty: float  # from actualQty
    avg_price: float  # from actualPrice
    reduce_only: bool
    close_position: bool
    stop_price: float | None  # from triggerPrice
    update_time_ms: int
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_rest_payload(cls, payload: dict[str, Any]) -> "AlgoOrderResult":
        algo_status = payload.get("algoStatus", "NEW")
        trigger_price = payload.get("triggerPrice")
        return cls(
            order_id=int(payload["algoId"]),
            client_order_id=str(payload.get("clientAlgoId", "")),
            symbol=payload["symbol"],
            side=payload["side"],
            type=payload.get("orderType", ""),
            status=_ALGO_STATUS_MAP.get(algo_status, algo_status),
            quantity=float(payload.get("quantity", 0) or 0),
            executed_qty=float(payload.get("actualQty", 0) or 0),
            avg_price=float(payload.get("actualPrice", 0) or 0),
            reduce_only=bool(payload.get("reduceOnly", False)),
            close_position=bool(payload.get("closePosition", False)),
            stop_price=float(trigger_price) if trigger_price not in (None, "0", "") else None,
            update_time_ms=int(payload.get("updateTime", 0) or 0),
            raw=payload,
        )


@dataclass(slots=True)
class PositionRisk:
    symbol: str
    position_amt: float  # signed: positive = long, negative = short, 0 = flat
    entry_price: float
    mark_price: float
    unrealized_pnl: float
    leverage: int
    liquidation_price: float
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_rest_payload(cls, payload: dict[str, Any]) -> "PositionRisk":
        return cls(
            symbol=payload["symbol"],
            position_amt=float(payload.get("positionAmt", 0) or 0),
            entry_price=float(payload.get("entryPrice", 0) or 0),
            mark_price=float(payload.get("markPrice", 0) or 0),
            unrealized_pnl=float(payload.get("unRealizedProfit", 0) or 0),
            leverage=int(float(payload.get("leverage", 1) or 1)),
            liquidation_price=float(payload.get("liquidationPrice", 0) or 0),
            raw=payload,
        )


@dataclass(slots=True)
class TickerStats:
    """24h rolling stats - spec section for the Market Scanner (Fase 14).
    Public, unauthenticated endpoint - no permission concerns."""
    symbol: str
    price_change_pct: float
    last_price: float
    quote_volume: float  # 24h volume in quote currency (USDT) - the real liquidity signal, not base-asset volume
    source: str = "binance"

    @classmethod
    def from_rest_payload(cls, payload: dict[str, Any]) -> "TickerStats":
        return cls(
            symbol=payload["symbol"],
            price_change_pct=float(payload.get("priceChangePercent", 0) or 0),
            last_price=float(payload.get("lastPrice", 0) or 0),
            quote_volume=float(payload.get("quoteVolume", 0) or 0),
        )
