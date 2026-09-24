"""Real order execution against BingX Perpetual Swap (Fase 16) - the BingX
counterpart to `aegis.execution.binance_provider.BinanceExecutionProvider`,
implementing the exact same duck-typed interface `ShadowTradingEngine`
already consumes (`open_bracket_position`, `get_position`,
`get_order_status`, `cancel_leftover_order`) so the entire decision
pipeline (Kill Switch, RiskEngine, Strategy Engine) stays byte-for-byte
identical regardless of which exchange actually executes - the
architectural principle this whole migration rests on.

Runs BingX in ONE-WAY position mode exclusively (never Hedge/dual-side),
mirroring how this project already uses Binance (no `positionSide` sent
there beyond the implicit default). This is a deliberate simplification,
not a missing feature: one-way mode makes `positionSide` always "BOTH" and
`positionAmt` a single signed value per symbol, exactly matching
`PositionRisk`'s existing semantics everywhere else in this codebase - a
hedge-mode account would need to track LONG and SHORT positions on the
same symbol independently, which nothing else in this system (RiskEngine,
ShadowTradingEngine, the `max_open_positions` limit) is designed to do.
`ensure_one_way_mode()` must be called once, against a flat account (no
open positions/orders - BingX rejects the switch otherwise), before this
provider is ever used to trade.

Same fail-safe policy as `BinanceExecutionProvider`: a position is only
ever left open if BOTH protective orders (stop-loss, take-profit) were
placed successfully. If either leg fails, the just-opened entry is
immediately flattened (market, reduce-only) rather than left running
unprotected or without its intended exit target.

Leverage is set explicitly before every entry, for the same reason
documented in `BinanceExecutionProvider`: RiskEngine's liquidation-distance
math is only meaningful if the real account leverage matches what it was
told, and nothing here should trust a symbol's leverage carried over from
a previous manual or automated setting. If setting leverage fails, the
entry never happens at all.
"""
from __future__ import annotations

from dataclasses import dataclass

from aegis.providers.bingx.models import OrderResult, PositionRisk
from aegis.providers.bingx.rest_client import BingXFuturesRestClient, BingXOrderError

_UNKNOWN_ORDER_CODE = 109421  # BingX: "order does not exist" - already gone, not a failure


@dataclass(slots=True)
class BracketOrders:
    entry: OrderResult
    stop: OrderResult
    take_profit: OrderResult


class BracketOpenError(RuntimeError):
    """Same shape as `aegis.execution.binance_provider.BracketOpenError` -
    `flattened` says whether the emergency close succeeded (True) or also
    failed (False - manual intervention is required immediately)."""

    def __init__(self, message: str, flattened: bool) -> None:
        super().__init__(message)
        self.flattened = flattened


class BingXExecutionProvider:
    def __init__(self, rest_client: BingXFuturesRestClient) -> None:
        self.rest = rest_client

    @staticmethod
    def _entry_side(side: str) -> str:
        return "BUY" if side == "LONG" else "SELL"

    @staticmethod
    def _closing_side(side: str) -> str:
        return "SELL" if side == "LONG" else "BUY"

    async def ensure_one_way_mode(self) -> None:
        """Call once, at startup, against a flat account - see module
        docstring. Idempotent: a no-op if already in one-way mode."""
        if await self.rest.get_position_mode():  # True = currently in hedge mode
            await self.rest.set_position_mode(hedge_mode=False)

    async def open_bracket_position(
        self, symbol: str, side: str, quantity: float, stop_price: float, take_profit_price: float, leverage: int,
    ) -> BracketOrders:
        await self._set_leverage_or_raise(symbol, leverage)
        entry = await self.rest.place_market_order(symbol, self._entry_side(side), quantity)
        closing_side = self._closing_side(side)
        fill_qty = entry.executed_qty or quantity

        try:
            stop = await self.rest.place_stop_market_order(symbol, closing_side, stop_price, quantity=fill_qty)
            take_profit = await self.rest.place_take_profit_market_order(
                symbol, closing_side, take_profit_price, quantity=fill_qty,
            )
        except BingXOrderError as exc:
            flattened = await self._emergency_close(symbol, closing_side, fill_qty)
            raise BracketOpenError(
                f"bracket setup failed after entry filled ({symbol} {side} qty={quantity}): {exc}. "
                + ("Position was flattened." if flattened
                   else "FLATTEN ALSO FAILED - MANUAL INTERVENTION REQUIRED NOW."),
                flattened=flattened,
            ) from exc

        return BracketOrders(entry=entry, stop=stop, take_profit=take_profit)

    async def _set_leverage_or_raise(self, symbol: str, leverage: int) -> None:
        try:
            await self.rest.set_leverage(symbol, leverage)
        except BingXOrderError as exc:
            raise BracketOpenError(
                f"could not set leverage to {leverage}x for {symbol} before entry: {exc}. No order was placed.",
                flattened=True,
            ) from exc

    async def _emergency_close(self, symbol: str, closing_side: str, quantity: float) -> bool:
        try:
            await self.rest.place_market_order(symbol, closing_side, quantity, reduce_only=True)
            return True
        except BingXOrderError:
            return False

    async def cancel_leftover_order(self, symbol: str, order_id: int) -> None:
        """Best-effort: an already-filled/canceled order (code 109421) IS
        the desired end state, not a failure, so it's swallowed rather than
        raised."""
        try:
            await self.rest.cancel_order(symbol, order_id)
        except BingXOrderError as exc:
            if exc.code != _UNKNOWN_ORDER_CODE:
                raise

    async def get_position(self, symbol: str) -> PositionRisk:
        positions = await self.rest.get_position_risk(symbol=symbol)
        if not positions:
            return PositionRisk(symbol=symbol, position_amt=0.0, entry_price=0.0, mark_price=0.0,
                                 unrealized_pnl=0.0, leverage=1, liquidation_price=0.0)
        return positions[0]

    async def get_order_status(self, symbol: str, order_id: int) -> OrderResult:
        return await self.rest.get_order(symbol, order_id)
