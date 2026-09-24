"""Typed representations of BingX Perpetual Swap responses - normalized to
the SAME attribute names `aegis.providers.binance.models` uses, so
`BingXExecutionProvider` and `BinanceExecutionProvider` can be used
interchangeably by `ShadowTradingEngine` without it caring which exchange
actually served a given call.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class SymbolRules:
    symbol: str  # our canonical form, e.g. BTCUSDT (not BingX's BTC-USDT)
    status: str
    price_precision: int
    quantity_precision: int
    tick_size: float
    step_size: float
    min_notional: float
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_contract_payload(cls, canonical_symbol: str, payload: dict[str, Any]) -> "SymbolRules":
        """BingX's "Get Contract Info" gives decimal-place counts
        (`quantityPrecision`/`pricePrecision`), not an explicit tick/step
        size like Binance's exchangeInfo filters - tick_size/step_size are
        derived as 10^-precision, which is exactly what those decimal
        places mean (e.g. quantityPrecision=3 => step_size=0.001)."""
        quantity_precision = int(payload.get("quantityPrecision", 0) or 0)
        price_precision = int(payload.get("pricePrecision", 0) or 0)
        status = "TRADING" if str(payload.get("status")) == "1" else "BREAK"
        return cls(
            symbol=canonical_symbol,
            status=status,
            price_precision=price_precision,
            quantity_precision=quantity_precision,
            tick_size=10 ** -price_precision if price_precision else 1.0,
            step_size=10 ** -quantity_precision if quantity_precision else 1.0,
            min_notional=float(payload.get("tradeMinUSDT", 0) or 0),
            raw=payload,
        )


@dataclass(slots=True)
class OrderResult:
    """Response from a signed order endpoint (place/cancel/query). BingX's
    field names differ slightly from Binance's (`orderID` string vs
    `orderId` int for ids beyond JS safe-integer range - Python ints don't
    have that precision problem, so the plain `orderId` int field is used
    directly), normalized here to the same shape `OrderResult` in
    `aegis.providers.binance.models` uses."""
    order_id: int
    client_order_id: str
    symbol: str  # our canonical form
    side: str
    type: str
    status: str
    quantity: float
    executed_qty: float
    avg_price: float
    reduce_only: bool
    close_position: bool
    stop_price: float | None
    update_time_ms: int
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_rest_payload(cls, canonical_symbol: str, payload: dict[str, Any]) -> "OrderResult":
        stop_price = payload.get("stopPrice")
        return cls(
            order_id=int(payload["orderId"]),
            client_order_id=str(payload.get("clientOrderId", "")),
            symbol=canonical_symbol,
            side=payload.get("side", ""),
            type=payload.get("type", ""),
            status=payload.get("status", "NEW"),
            quantity=float(payload.get("origQty", 0) or 0),
            # Place Order's own ACK does not always echo executedQty/avgPrice
            # (see this provider's module docstring) - 0.0 here is a real,
            # meaningful "not reported by this call" value, matching how
            # ShadowTradingEngine already falls back (brackets.entry.avg_price
            # or a reference price) when a real fill price isn't available.
            executed_qty=float(payload.get("executedQty", 0) or 0),
            avg_price=float(payload.get("avgPrice", 0) or 0),
            reduce_only=bool(payload.get("reduceOnly", False)),
            close_position=bool(payload.get("closePosition", False)),
            stop_price=float(stop_price) if stop_price not in (None, "0", "", 0) else None,
            update_time_ms=int(payload.get("updateTime", 0) or 0),
            raw=payload,
        )


@dataclass(slots=True)
class PositionRisk:
    symbol: str  # our canonical form
    position_amt: float  # signed: positive = long, negative = short, 0 = flat
    entry_price: float
    mark_price: float
    unrealized_pnl: float
    leverage: int
    liquidation_price: float
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_rest_payload(cls, canonical_symbol: str, payload: dict[str, Any]) -> "PositionRisk":
        # BingX reports positionAmt as an UNSIGNED magnitude with a separate
        # positionSide field (LONG/SHORT/BOTH) - unlike Binance, where a
        # signed positionAmt alone encodes direction in one-way mode. This
        # provider always runs the account in one-way mode (see
        # BingXExecutionProvider docstring), where positionSide is "BOTH"
        # and BingX's own positionAmt sign already matches Binance's
        # convention directly; LONG/SHORT are only handled here as a
        # defensive fallback in case an account is ever left in hedge mode.
        amt = float(payload.get("positionAmt", 0) or 0)
        side = payload.get("positionSide", "BOTH")
        if side == "SHORT":
            amt = -abs(amt)
        elif side == "LONG":
            amt = abs(amt)
        return cls(
            symbol=canonical_symbol,
            position_amt=amt,
            entry_price=float(payload.get("avgPrice", 0) or 0),
            mark_price=float(payload.get("markPrice", 0) or 0),
            unrealized_pnl=float(payload.get("unrealizedProfit", 0) or 0),
            leverage=int(float(payload.get("leverage", 1) or 1)),
            liquidation_price=float(payload.get("liquidationPrice", 0) or 0),
            raw=payload,
        )
