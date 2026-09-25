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

from aegis.execution.models import BracketOpenError  # noqa: F401 - re-exported, see below
from aegis.providers.bingx.models import OrderResult, PositionRisk
from aegis.providers.bingx.rest_client import BingXFuturesRestClient, BingXOrderError

# BracketOpenError moved to aegis.execution.models (Fase 17d) so it's the
# SAME class BinanceExecutionProvider raises - re-exported here so existing
# code/tests importing `from aegis.execution.bingx_provider import
# BracketOpenError` keep working unchanged. This used to be a SEPARATE
# look-alike class, which meant ShadowTradingEngine/MomentumTradingEngine's
# `except BracketOpenError` (bound to Binance's copy) never caught the one
# BingXExecutionProvider actually raised - see models.py's docstring for
# the real crash this caused.
_UNKNOWN_ORDER_CODE = 109421  # BingX: "order does not exist" - already gone, not a failure


@dataclass(slots=True)
class BracketOrders:
    entry: OrderResult
    stop: OrderResult
    take_profit: OrderResult


@dataclass(slots=True)
class TrailingBracketOrders:
    """Same shape as BracketOrders, but the profit-taking leg is a
    TRAILING_STOP_MARKET instead of a fixed take-profit - Fase 16's BingX
    Momentum Engine, mirroring `aegis.execution.binance_provider.TrailingBracketOrders`."""
    entry: OrderResult
    stop: OrderResult
    trailing_stop: OrderResult


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

    async def get_equity(self) -> float:
        """Real total equity (wallet balance + unrealized PnL), reported by
        BingX itself - the position-sizing ground truth (Fase 17b). Found
        live 2026-09-25 on the ALREADY-RUNNING demo account: the locally
        tracked `risk_account_state.equity` (hardcoded to start at 1000.0)
        had already drifted from the real VST balance (987.14 + 1.31
        unrealized = 988.46) - harmless in demo, but sizing every position
        off a fictitious number instead of the account's real size would be
        a serious miscalibration the moment real money is involved (a $100
        real deposit would otherwise be sized as if it were $1000, a 10x
        error). BingX's Perpetual Swap balance endpoint returns a single-
        asset list (VST for demo, USDT for real) with `equity` already
        computed server-side - verified live against the demo endpoint."""
        balance = await self.rest.get_account_balance()
        if not balance:
            raise ValueError("BingX get_account_balance() returned no entries - cannot determine real equity")
        return float(balance[0]["equity"])

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

    async def open_trailing_bracket_position(
        self, symbol: str, side: str, quantity: float, stop_price: float, callback_rate_pct: float, leverage: int,
        activation_price: float | None = None,
    ) -> TrailingBracketOrders:
        """Same entry + safety-net-stop mechanics as `open_bracket_position`,
        but the profit-taking leg is a TRAILING_STOP_MARKET (Fase 16 -
        BingX Momentum Engine) instead of a fixed take-profit target -
        mirrors `BinanceExecutionProvider.open_trailing_bracket_position`.
        Same fail-safe policy: either leg failing to place flattens the
        position immediately, never left running with partial or no
        protection."""
        await self._set_leverage_or_raise(symbol, leverage)
        entry = await self.rest.place_market_order(symbol, self._entry_side(side), quantity)
        closing_side = self._closing_side(side)
        fill_qty = entry.executed_qty or quantity

        try:
            stop = await self.rest.place_stop_market_order(symbol, closing_side, stop_price, quantity=fill_qty)
            trailing_stop = await self.rest.place_trailing_stop_order(
                symbol, closing_side, callback_rate_pct, quantity=fill_qty, activation_price=activation_price,
            )
        except BingXOrderError as exc:
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
        """Best-effort: an already-filled/canceled order IS the desired end
        state, not a failure, so it's swallowed rather than raised - same
        principle as `BinanceExecutionProvider.cancel_leftover_order`.

        Found live (2026-09-24) that this is NOT reliably code 109421
        ("The specified order does not exist") as documented: when one
        bracket leg fills, BingX auto-removes the sibling leg itself
        (standard OCO-style behavior), and cancelling that already-gone
        sibling actually comes back as code 109400 (BingX's generic
        "invalid parameters" catch-all) with message "order not exist" -
        a real, observed inconsistency in BingX's own error taxonomy, not
        a hypothetical. This crashed the whole polling process every
        cycle it hit a stale leftover order - the local position record
        was never marked closed because the crash happened before that
        step ran, which is exactly why a position already flat on the
        exchange kept showing as open on the dashboard indefinitely.
        Matches on the message text (case-insensitively), not just the
        code, mirroring how the Binance provider matches "Unknown order"
        in its own message rather than trusting a single fixed code."""
        try:
            await self.rest.cancel_order(symbol, order_id)
        except BingXOrderError as exc:
            if exc.code != _UNKNOWN_ORDER_CODE and "order not exist" not in str(exc).lower():
                raise

    async def get_position(self, symbol: str) -> PositionRisk:
        positions = await self.rest.get_position_risk(symbol=symbol)
        if not positions:
            return PositionRisk(symbol=symbol, position_amt=0.0, entry_price=0.0, mark_price=0.0,
                                 unrealized_pnl=0.0, leverage=1, liquidation_price=0.0)
        return positions[0]

    async def get_order_status(self, symbol: str, order_id: int) -> OrderResult:
        return await self.rest.get_order(symbol, order_id)
