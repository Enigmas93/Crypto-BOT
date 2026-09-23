"""Tests BinanceExecutionProvider against a fake REST client - especially
the safety-critical failure paths (a bracket leg failing to place must
never leave a naked, unprotected position open).
"""
from __future__ import annotations

import pytest

from aegis.execution.binance_provider import BinanceExecutionProvider, BracketOpenError, TrailingBracketOrders
from aegis.providers.binance.models import OrderResult, PositionRisk
from aegis.providers.binance.rest_client import BinanceOrderError


def _order(order_id: int, symbol="BTCUSDT", side="BUY", type_="MARKET", status="FILLED",
           qty=0.01, avg_price=65000.0) -> OrderResult:
    return OrderResult(
        order_id=order_id, client_order_id=f"aegis-{order_id}", symbol=symbol, side=side, type=type_,
        status=status, quantity=qty, executed_qty=qty, avg_price=avg_price, reduce_only=False,
        close_position=False, stop_price=None, update_time_ms=1700000000000,
    )


class _FakeRestClient:
    def __init__(self):
        self.calls: list[tuple] = []
        self.fail_stop = False
        self.fail_take_profit = False
        self.fail_trailing_stop = False
        self.fail_emergency_close = False
        self.fail_set_leverage = False
        self.fail_cancel_message: str | None = None
        self._next_order_id = 1

    def _new_id(self) -> int:
        self._next_order_id += 1
        return self._next_order_id

    async def set_leverage(self, symbol, leverage):
        self.calls.append(("set_leverage", symbol, leverage))
        if self.fail_set_leverage:
            raise BinanceOrderError("set leverage failed")
        return {"symbol": symbol, "leverage": leverage, "maxNotionalValue": "1000000"}

    async def place_market_order(self, symbol, side, quantity, reduce_only=False, client_order_id=None):
        self.calls.append(("place_market_order", symbol, side, quantity, reduce_only))
        if reduce_only and self.fail_emergency_close:
            raise BinanceOrderError("emergency close failed")
        return _order(self._new_id(), symbol=symbol, side=side, qty=quantity)

    async def place_stop_market_order(self, symbol, side, stop_price, quantity=None,
                                       close_position=False, client_order_id=None):
        self.calls.append(("place_stop_market_order", symbol, side, stop_price))
        if self.fail_stop:
            raise BinanceOrderError("stop rejected")
        return _order(self._new_id(), symbol=symbol, side=side, type_="STOP_MARKET", status="NEW")

    async def place_take_profit_market_order(self, symbol, side, stop_price, quantity=None,
                                              close_position=False, client_order_id=None):
        self.calls.append(("place_take_profit_market_order", symbol, side, stop_price))
        if self.fail_take_profit:
            raise BinanceOrderError("take profit rejected")
        return _order(self._new_id(), symbol=symbol, side=side, type_="TAKE_PROFIT_MARKET", status="NEW")

    async def place_trailing_stop_order(self, symbol, side, callback_rate_pct, quantity,
                                         activation_price=None, client_order_id=None):
        self.calls.append(("place_trailing_stop_order", symbol, side, callback_rate_pct, quantity, activation_price))
        if self.fail_trailing_stop:
            raise BinanceOrderError("trailing stop rejected")
        return _order(self._new_id(), symbol=symbol, side=side, type_="TRAILING_STOP_MARKET", status="NEW")

    async def cancel_algo_order(self, symbol, algo_id):
        self.calls.append(("cancel_algo_order", symbol, algo_id))
        if self.fail_cancel_message:
            raise BinanceOrderError(self.fail_cancel_message)
        # real DELETE /fapi/v1/algoOrder returns a minimal confirmation,
        # not a full order object - see rest_client.cancel_algo_order
        return {"algoId": algo_id, "clientAlgoId": f"aegis-{algo_id}", "code": "200", "msg": "success"}

    async def get_algo_order(self, symbol, algo_id):
        self.calls.append(("get_algo_order", symbol, algo_id))
        return _order(algo_id, symbol=symbol, status="NEW")

    async def get_position_risk(self, symbol=None):
        self.calls.append(("get_position_risk", symbol))
        return getattr(self, "_position_risk_response", [])


@pytest.mark.asyncio
async def test_open_bracket_position_happy_path():
    rest = _FakeRestClient()
    provider = BinanceExecutionProvider(rest)

    brackets = await provider.open_bracket_position("BTCUSDT", "LONG", 0.01, 63000.0, 68000.0, 3)

    assert brackets.entry.side == "BUY"
    assert brackets.stop.type == "STOP_MARKET"
    assert brackets.take_profit.type == "TAKE_PROFIT_MARKET"
    # closing side for a LONG's protective orders must be SELL
    assert brackets.stop.side == "SELL"
    assert brackets.take_profit.side == "SELL"


@pytest.mark.asyncio
async def test_open_bracket_position_short_uses_sell_entry_and_buy_exits():
    rest = _FakeRestClient()
    provider = BinanceExecutionProvider(rest)

    brackets = await provider.open_bracket_position("BTCUSDT", "SHORT", 0.01, 68000.0, 63000.0, 3)

    assert brackets.entry.side == "SELL"
    assert brackets.stop.side == "BUY"
    assert brackets.take_profit.side == "BUY"


@pytest.mark.asyncio
async def test_stop_leg_failure_flattens_the_position_and_raises():
    rest = _FakeRestClient()
    rest.fail_stop = True
    provider = BinanceExecutionProvider(rest)

    with pytest.raises(BracketOpenError) as exc_info:
        await provider.open_bracket_position("BTCUSDT", "LONG", 0.01, 63000.0, 68000.0, 3)

    assert exc_info.value.flattened is True
    # the emergency close must be a reduce-only market order on the closing side
    emergency_calls = [c for c in rest.calls if c[0] == "place_market_order" and c[4] is True]
    assert len(emergency_calls) == 1
    assert emergency_calls[0][2] == "SELL"  # closing a LONG


@pytest.mark.asyncio
async def test_take_profit_leg_failure_flattens_the_position_and_raises():
    rest = _FakeRestClient()
    rest.fail_take_profit = True
    provider = BinanceExecutionProvider(rest)

    with pytest.raises(BracketOpenError) as exc_info:
        await provider.open_bracket_position("BTCUSDT", "SHORT", 0.01, 68000.0, 63000.0, 3)

    assert exc_info.value.flattened is True
    emergency_calls = [c for c in rest.calls if c[0] == "place_market_order" and c[4] is True]
    assert len(emergency_calls) == 1
    assert emergency_calls[0][2] == "BUY"  # closing a SHORT


@pytest.mark.asyncio
async def test_when_flatten_also_fails_the_error_says_so_explicitly():
    rest = _FakeRestClient()
    rest.fail_stop = True
    rest.fail_emergency_close = True
    provider = BinanceExecutionProvider(rest)

    with pytest.raises(BracketOpenError) as exc_info:
        await provider.open_bracket_position("BTCUSDT", "LONG", 0.01, 63000.0, 68000.0, 3)

    assert exc_info.value.flattened is False
    assert "MANUAL INTERVENTION" in str(exc_info.value)


@pytest.mark.asyncio
async def test_cancel_leftover_order_swallows_unknown_order_error():
    rest = _FakeRestClient()
    rest.fail_cancel_message = "Unknown order sent."
    provider = BinanceExecutionProvider(rest)

    await provider.cancel_leftover_order("BTCUSDT", 42)  # must not raise


@pytest.mark.asyncio
async def test_cancel_leftover_order_propagates_unexpected_errors():
    rest = _FakeRestClient()
    rest.fail_cancel_message = "Internal server error"
    provider = BinanceExecutionProvider(rest)

    with pytest.raises(BinanceOrderError):
        await provider.cancel_leftover_order("BTCUSDT", 42)


@pytest.mark.asyncio
async def test_get_position_returns_flat_placeholder_when_exchange_has_no_row():
    rest = _FakeRestClient()
    rest._position_risk_response = []
    provider = BinanceExecutionProvider(rest)

    position = await provider.get_position("BTCUSDT")

    assert position.position_amt == 0.0
    assert position.symbol == "BTCUSDT"


@pytest.mark.asyncio
async def test_get_position_returns_the_real_row_when_present():
    rest = _FakeRestClient()
    rest._position_risk_response = [
        PositionRisk(symbol="BTCUSDT", position_amt=0.01, entry_price=65000.0, mark_price=65200.0,
                     unrealized_pnl=2.0, leverage=3, liquidation_price=43000.0),
    ]
    provider = BinanceExecutionProvider(rest)

    position = await provider.get_position("BTCUSDT")
    assert position.position_amt == pytest.approx(0.01)


@pytest.mark.asyncio
async def test_open_trailing_bracket_position_happy_path():
    rest = _FakeRestClient()
    provider = BinanceExecutionProvider(rest)

    brackets = await provider.open_trailing_bracket_position(
        "BTCUSDT", "LONG", 0.01, 63000.0, 2.0, 3, activation_price=66000.0,
    )

    assert isinstance(brackets, TrailingBracketOrders)
    assert brackets.entry.side == "BUY"
    assert brackets.stop.type == "STOP_MARKET"
    assert brackets.stop.side == "SELL"
    assert brackets.trailing_stop.type == "TRAILING_STOP_MARKET"
    assert brackets.trailing_stop.side == "SELL"
    trailing_calls = [c for c in rest.calls if c[0] == "place_trailing_stop_order"]
    assert trailing_calls == [("place_trailing_stop_order", "BTCUSDT", "SELL", 2.0, 0.01, 66000.0)]


@pytest.mark.asyncio
async def test_open_trailing_bracket_position_defaults_activation_price_to_none():
    rest = _FakeRestClient()
    provider = BinanceExecutionProvider(rest)

    await provider.open_trailing_bracket_position("BTCUSDT", "LONG", 0.01, 63000.0, 2.0, 3)

    trailing_calls = [c for c in rest.calls if c[0] == "place_trailing_stop_order"]
    assert trailing_calls[0][5] is None


@pytest.mark.asyncio
async def test_trailing_stop_leg_failure_flattens_and_raises():
    rest = _FakeRestClient()
    rest.fail_trailing_stop = True
    provider = BinanceExecutionProvider(rest)

    with pytest.raises(BracketOpenError) as exc_info:
        await provider.open_trailing_bracket_position("BTCUSDT", "LONG", 0.01, 63000.0, 2.0, 3)

    assert exc_info.value.flattened is True
    emergency_calls = [c for c in rest.calls if c[0] == "place_market_order" and c[4] is True]
    assert len(emergency_calls) == 1


@pytest.mark.asyncio
async def test_trailing_bracket_hard_stop_failure_also_flattens():
    rest = _FakeRestClient()
    rest.fail_stop = True
    provider = BinanceExecutionProvider(rest)

    with pytest.raises(BracketOpenError) as exc_info:
        await provider.open_trailing_bracket_position("BTCUSDT", "SHORT", 0.01, 68000.0, 2.0, 3)

    assert exc_info.value.flattened is True
    # trailing stop must never be placed if the hard stop already failed
    assert not any(c[0] == "place_trailing_stop_order" for c in rest.calls)


@pytest.mark.asyncio
async def test_set_leverage_is_called_before_the_entry_order():
    rest = _FakeRestClient()
    provider = BinanceExecutionProvider(rest)

    await provider.open_bracket_position("BTCUSDT", "LONG", 0.01, 63000.0, 68000.0, 5)

    action_order = [c[0] for c in rest.calls]
    assert action_order.index("set_leverage") < action_order.index("place_market_order")
    leverage_calls = [c for c in rest.calls if c[0] == "set_leverage"]
    assert leverage_calls == [("set_leverage", "BTCUSDT", 5)]


@pytest.mark.asyncio
async def test_set_leverage_failure_prevents_any_order_from_being_placed():
    rest = _FakeRestClient()
    rest.fail_set_leverage = True
    provider = BinanceExecutionProvider(rest)

    with pytest.raises(BracketOpenError) as exc_info:
        await provider.open_bracket_position("BTCUSDT", "LONG", 0.01, 63000.0, 68000.0, 5)

    assert exc_info.value.flattened is True  # nothing was ever opened
    assert not any(c[0] == "place_market_order" for c in rest.calls)


@pytest.mark.asyncio
async def test_set_leverage_failure_prevents_trailing_bracket_entry_too():
    rest = _FakeRestClient()
    rest.fail_set_leverage = True
    provider = BinanceExecutionProvider(rest)

    with pytest.raises(BracketOpenError):
        await provider.open_trailing_bracket_position("BTCUSDT", "LONG", 0.01, 63000.0, 2.0, 5)

    assert not any(c[0] == "place_market_order" for c in rest.calls)
