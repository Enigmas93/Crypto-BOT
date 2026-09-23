"""Real order execution against Binance Futures (Phase 10) - the
`ExecutionProvider` spec section 120 refers to. Wraps the signed REST
endpoints into an open/close-a-bracket-position shape, so
`ShadowTradingEngine`'s decision logic (Kill Switch, RiskEngine, Strategy
Engine) can stay identical to `PaperTradingEngine`'s - only what happens
AFTER a decision is made differs, exactly as spec section 120 intends.

Safety policy: a position is only ever left open if BOTH its protective
orders (stop-loss, take-profit) were placed successfully. If either leg
fails, the just-opened entry is immediately flattened (market, reduce-only)
rather than left running unprotected or without its intended exit target -
spec section 110 ("never a position without a stop"), enforced here at the
point of real execution, not just as a RiskEngine precondition. This is a
deliberately conservative choice: a partially-protected position (e.g. a
stop but no take-profit) is NOT kept running - capital preservation over
letting a partial setup ride (spec's stated philosophy throughout).

Leverage is set explicitly, every time, before the entry order - found
live that skipping this leaves a symbol at whatever leverage the account
already had (Binance's own per-symbol default, e.g. 20x on a fresh testnet
account for SOLUSDT, discovered when the real exchange UI showed 20x for
a position RiskEngine had sized assuming 3x). RiskEngine's liquidation-
distance safety check (`estimate_liquidation_distance_pct`) is only
meaningful if the real leverage matches what it was told - silently
trading at a different leverage would make that entire safety check a
comforting lie, not a real one. If setting leverage fails, the entry never
happens at all - there is nothing to flatten yet, so this is the one
failure mode in this module that costs nothing to be strict about.
"""
from __future__ import annotations

from dataclasses import dataclass

from aegis.providers.binance.models import AlgoOrderResult, OrderResult, PositionRisk
from aegis.providers.binance.rest_client import BinanceFuturesRestClient, BinanceOrderError


@dataclass(slots=True)
class BracketOrders:
    entry: OrderResult
    stop: AlgoOrderResult
    take_profit: AlgoOrderResult


@dataclass(slots=True)
class TrailingBracketOrders:
    """Same shape as BracketOrders, but the profit-taking leg is a
    TRAILING_STOP_MARKET (ratchets in the position's favor, never against)
    instead of a fixed-price take-profit - Fase 14's Momentum Engine."""
    entry: OrderResult
    stop: AlgoOrderResult
    trailing_stop: AlgoOrderResult


class BracketOpenError(RuntimeError):
    """Raised when a bracket could not be fully established. `flattened`
    says whether the emergency close succeeded (True) or also failed
    (False - manual intervention is required immediately)."""

    def __init__(self, message: str, flattened: bool) -> None:
        super().__init__(message)
        self.flattened = flattened


class BinanceExecutionProvider:
    def __init__(self, rest_client: BinanceFuturesRestClient) -> None:
        self.rest = rest_client

    @staticmethod
    def _entry_side(side: str) -> str:
        return "BUY" if side == "LONG" else "SELL"

    @staticmethod
    def _closing_side(side: str) -> str:
        return "SELL" if side == "LONG" else "BUY"

    async def open_bracket_position(
        self, symbol: str, side: str, quantity: float, stop_price: float, take_profit_price: float, leverage: int,
    ) -> BracketOrders:
        await self._set_leverage_or_raise(symbol, leverage)
        entry = await self.rest.place_market_order(symbol, self._entry_side(side), quantity)
        closing_side = self._closing_side(side)
        # Explicit quantity + reduceOnly, not closePosition=true: verified
        # live against Binance Futures Testnet that closePosition=true on
        # STOP_MARKET/TAKE_PROFIT_MARKET returns -4120 ("Order type not
        # supported for this endpoint... use the Algo Order API instead")
        # via the standard /fapi/v1/order endpoint. quantity+reduceOnly is
        # also a better fit here regardless: we always know the exact
        # quantity we opened, so there's no ambiguity closePosition would
        # need to resolve.
        fill_qty = entry.executed_qty or quantity

        try:
            stop = await self.rest.place_stop_market_order(symbol, closing_side, stop_price, quantity=fill_qty)
            take_profit = await self.rest.place_take_profit_market_order(
                symbol, closing_side, take_profit_price, quantity=fill_qty,
            )
        except BinanceOrderError as exc:
            flattened = await self._emergency_close(symbol, closing_side, entry.executed_qty or quantity)
            raise BracketOpenError(
                f"bracket setup failed after entry filled ({symbol} {side} qty={quantity}): {exc}. "
                + ("Position was flattened." if flattened
                   else "FLATTEN ALSO FAILED - MANUAL INTERVENTION REQUIRED NOW."),
                flattened=flattened,
            ) from exc

        return BracketOrders(entry=entry, stop=stop, take_profit=take_profit)

    async def open_trailing_bracket_position(
        self, symbol: str, side: str, quantity: float, stop_price: float, callback_rate_pct: float, leverage: int,
        activation_price: float | None = None,
    ) -> TrailingBracketOrders:
        """Same entry + safety-net-stop mechanics as `open_bracket_position`,
        but the profit-taking leg is a TRAILING_STOP_MARKET (Fase 14 -
        Momentum Engine) instead of a fixed take-profit target - lets a
        genuinely strong move keep running instead of capping it at a
        pre-set R-multiple, while `stop_price` still guarantees a hard exit
        if the trailing leg never gets the chance to protect anything (e.g.
        price gaps straight through it). Same fail-safe policy as the fixed
        bracket: either leg failing to place flattens the position
        immediately, never left running with partial or no protection."""
        await self._set_leverage_or_raise(symbol, leverage)
        entry = await self.rest.place_market_order(symbol, self._entry_side(side), quantity)
        closing_side = self._closing_side(side)
        fill_qty = entry.executed_qty or quantity

        try:
            stop = await self.rest.place_stop_market_order(symbol, closing_side, stop_price, quantity=fill_qty)
            trailing_stop = await self.rest.place_trailing_stop_order(
                symbol, closing_side, callback_rate_pct, quantity=fill_qty, activation_price=activation_price,
            )
        except BinanceOrderError as exc:
            flattened = await self._emergency_close(symbol, closing_side, fill_qty)
            raise BracketOpenError(
                f"trailing bracket setup failed after entry filled ({symbol} {side} qty={quantity}): {exc}. "
                + ("Position was flattened." if flattened
                   else "FLATTEN ALSO FAILED - MANUAL INTERVENTION REQUIRED NOW."),
                flattened=flattened,
            ) from exc

        return TrailingBracketOrders(entry=entry, stop=stop, trailing_stop=trailing_stop)

    async def _set_leverage_or_raise(self, symbol: str, leverage: int) -> None:
        try:
            await self.rest.set_leverage(symbol, leverage)
        except BinanceOrderError as exc:
            # Nothing was opened yet - "flattened=True" here means exactly
            # that: there is no dangling exposure to clean up, not that an
            # emergency close ran.
            raise BracketOpenError(
                f"could not set leverage to {leverage}x for {symbol} before entry: {exc}. No order was placed.",
                flattened=True,
            ) from exc

    async def _emergency_close(self, symbol: str, closing_side: str, quantity: float) -> bool:
        try:
            await self.rest.place_market_order(symbol, closing_side, quantity, reduce_only=True)
            return True
        except BinanceOrderError:
            return False

    async def cancel_leftover_order(self, symbol: str, order_id: int) -> None:
        """Best-effort: an 'unknown order' response means it's already gone
        (filled or previously cancelled) - that IS the desired end state,
        not a failure, so it's swallowed rather than raised. The stop/TP
        legs are always Algo Orders (see rest_client's module docstring),
        so this always goes through the algo cancel endpoint."""
        try:
            await self.rest.cancel_algo_order(symbol, order_id)
        except BinanceOrderError as exc:
            if "Unknown order" not in str(exc):
                raise

    async def get_position(self, symbol: str) -> PositionRisk:
        positions = await self.rest.get_position_risk(symbol=symbol)
        if not positions:
            return PositionRisk(symbol=symbol, position_amt=0.0, entry_price=0.0, mark_price=0.0,
                                 unrealized_pnl=0.0, leverage=1, liquidation_price=0.0)
        return positions[0]

    async def get_order_status(self, symbol: str, order_id: int) -> AlgoOrderResult:
        """For a stop/take-profit leg (always an Algo Order)."""
        return await self.rest.get_algo_order(symbol, order_id)
