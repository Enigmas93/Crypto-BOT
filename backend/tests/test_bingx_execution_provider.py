"""Tests BingXExecutionProvider against a fake REST client - mirrors
tests/test_binance_execution_provider.py so both providers are held to the
same safety bar (a bracket leg failing to place must never leave a naked,
unprotected position open).
"""
from __future__ import annotations

import pytest

from aegis.execution.bingx_provider import BingXExecutionProvider, BracketOpenError
from aegis.providers.bingx.models import OrderResult, PositionRisk
from aegis.providers.bingx.rest_client import BingXOrderError


def _order(order_id: int, symbol="BTCUSDT", side="BUY", type_="MARKET", status="FILLED",
           qty=0.01, avg_price=65000.0) -> OrderResult:
    return OrderResult(
        order_id=order_id, client_order_id=f"aegis{order_id}", symbol=symbol, side=side, type=type_,
        status=status, quantity=qty, executed_qty=qty, avg_price=avg_price, reduce_only=False,
        close_position=False, stop_price=None, update_time_ms=1700000000000,
    )


class _FakeRestClient:
    def __init__(self):
        self.calls: list[tuple] = []
        self.fail_stop = False
        self.fail_take_profit = False
        self.fail_emergency_close = False
        self.fail_set_leverage = False
        self.fail_cancel_code: int | None = None
        self._next_order_id = 1

    def _new_id(self) -> int:
        self._next_order_id += 1
        return self._next_order_id

    async def set_leverage(self, symbol, leverage):
        self.calls.append(("set_leverage", symbol, leverage))
        if self.fail_set_leverage:
            raise BingXOrderError("set leverage failed")
        return {"symbol": symbol, "leverage": leverage}

    async def place_market_order(self, symbol, side, quantity, reduce_only=False, client_order_id=None):
        self.calls.append(("place_market_order", symbol, side, quantity, reduce_only))
        if reduce_only and self.fail_emergency_close:
            raise BingXOrderError("emergency close failed")
        return _order(self._new_id(), symbol=symbol, side=side, qty=quantity)

    async def place_stop_market_order(self, symbol, side, stop_price, quantity):
        self.calls.append(("place_stop_market_order", symbol, side, stop_price))
        if self.fail_stop:
            raise BingXOrderError("stop rejected")
        return _order(self._new_id(), symbol=symbol, side=side, type_="STOP_MARKET", status="NEW")

    async def place_take_profit_market_order(self, symbol, side, stop_price, quantity):
        self.calls.append(("place_take_profit_market_order", symbol, side, stop_price))
        if self.fail_take_profit:
            raise BingXOrderError("take profit rejected")
        return _order(self._new_id(), symbol=symbol, side=side, type_="TAKE_PROFIT_MARKET", status="NEW")

    async def cancel_order(self, symbol, order_id):
        self.calls.append(("cancel_order", symbol, order_id))
        if self.fail_cancel_code is not None:
            raise BingXOrderError("cancel failed", code=self.fail_cancel_code)
        return _order(order_id, symbol=symbol, status="CANCELED")

    async def get_order(self, symbol, order_id):
        self.calls.append(("get_order", symbol, order_id))
        return _order(order_id, symbol=symbol, status="NEW")

    async def get_position_risk(self, symbol=None):
        self.calls.append(("get_position_risk", symbol))
        return getattr(self, "_position_risk_response", [])


@pytest.mark.asyncio
async def test_open_bracket_position_happy_path():
    rest = _FakeRestClient()
    provider = BingXExecutionProvider(rest)

    brackets = await provider.open_bracket_position("BTCUSDT", "LONG", 0.01, 63000.0, 68000.0, 3)

    assert brackets.entry.side == "BUY"
    assert brackets.stop.type == "STOP_MARKET"
    assert brackets.take_profit.type == "TAKE_PROFIT_MARKET"
    assert brackets.stop.side == "SELL"
    assert brackets.take_profit.side == "SELL"


@pytest.mark.asyncio
async def test_open_bracket_position_short_uses_sell_entry_and_buy_exits():
    rest = _FakeRestClient()
    provider = BingXExecutionProvider(rest)

    brackets = await provider.open_bracket_position("BTCUSDT", "SHORT", 0.01, 68000.0, 63000.0, 3)

    assert brackets.entry.side == "SELL"
    assert brackets.stop.side == "BUY"
    assert brackets.take_profit.side == "BUY"


@pytest.mark.asyncio
async def test_stop_leg_failure_flattens_the_position_and_raises():
    rest = _FakeRestClient()
    rest.fail_stop = True
    provider = BingXExecutionProvider(rest)

    with pytest.raises(BracketOpenError) as exc_info:
        await provider.open_bracket_position("BTCUSDT", "LONG", 0.01, 63000.0, 68000.0, 3)

    assert exc_info.value.flattened is True
    emergency_calls = [c for c in rest.calls if c[0] == "place_market_order" and c[4] is True]
    assert len(emergency_calls) == 1
    assert emergency_calls[0][2] == "SELL"  # closing a LONG


@pytest.mark.asyncio
async def test_take_profit_leg_failure_flattens_the_position_and_raises():
    rest = _FakeRestClient()
    rest.fail_take_profit = True
    provider = BingXExecutionProvider(rest)

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
    provider = BingXExecutionProvider(rest)

    with pytest.raises(BracketOpenError) as exc_info:
        await provider.open_bracket_position("BTCUSDT", "LONG", 0.01, 63000.0, 68000.0, 3)

    assert exc_info.value.flattened is False
    assert "MANUAL INTERVENTION" in str(exc_info.value)


@pytest.mark.asyncio
async def test_cancel_leftover_order_swallows_order_not_exist_code():
    rest = _FakeRestClient()
    rest.fail_cancel_code = 109421
    provider = BingXExecutionProvider(rest)

    await provider.cancel_leftover_order("BTCUSDT", 42)  # must not raise


@pytest.mark.asyncio
async def test_cancel_leftover_order_propagates_unexpected_error_codes():
    rest = _FakeRestClient()
    rest.fail_cancel_code = 100500
    provider = BingXExecutionProvider(rest)

    with pytest.raises(BingXOrderError):
        await provider.cancel_leftover_order("BTCUSDT", 42)


@pytest.mark.asyncio
async def test_get_position_returns_flat_placeholder_when_exchange_has_no_row():
    rest = _FakeRestClient()
    rest._position_risk_response = []
    provider = BingXExecutionProvider(rest)

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
    provider = BingXExecutionProvider(rest)

    position = await provider.get_position("BTCUSDT")
    assert position.position_amt == pytest.approx(0.01)


@pytest.mark.asyncio
async def test_set_leverage_is_called_before_the_entry_order():
    rest = _FakeRestClient()
    provider = BingXExecutionProvider(rest)

    await provider.open_bracket_position("BTCUSDT", "LONG", 0.01, 63000.0, 68000.0, 5)

    action_order = [c[0] for c in rest.calls]
    assert action_order.index("set_leverage") < action_order.index("place_market_order")
    leverage_calls = [c for c in rest.calls if c[0] == "set_leverage"]
    assert leverage_calls == [("set_leverage", "BTCUSDT", 5)]


@pytest.mark.asyncio
async def test_set_leverage_failure_prevents_any_order_from_being_placed():
    rest = _FakeRestClient()
    rest.fail_set_leverage = True
    provider = BingXExecutionProvider(rest)

    with pytest.raises(BracketOpenError) as exc_info:
        await provider.open_bracket_position("BTCUSDT", "LONG", 0.01, 63000.0, 68000.0, 5)

    assert exc_info.value.flattened is True  # nothing was ever opened
    assert not any(c[0] == "place_market_order" for c in rest.calls)
