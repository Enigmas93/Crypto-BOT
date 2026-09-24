"""Integration tests for PaperRepository against a real TimescaleDB.
Skipped automatically if the database is unreachable (see conftest.py).
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from aegis.db.paper_repository import PaperRepository
from aegis.execution.models import OpenPosition
from aegis.paper.models import PaperTrade

_T0 = datetime(2026, 1, 1, tzinfo=UTC)
_T1 = datetime(2026, 1, 1, 1, tzinfo=UTC)


def _account_id() -> str:
    return f"testpaper_{uuid.uuid4().hex[:8]}"


def _position() -> OpenPosition:
    return OpenPosition(side="LONG", entry_time=_T0, entry_price=100.0, stop_price=96.0,
                         take_profit_price=108.0, quantity=1.0, risk_amount=4.0,
                         confluence_score=45.0, reasons=["breakout_confirmed"])


def _trade(account_id: str) -> PaperTrade:
    return PaperTrade(account_id=account_id, symbol="BTCUSDT", side="LONG", entry_time=_T0, entry_price=100.0,
                       exit_time=_T1, exit_price=108.0, exit_reason="TAKE_PROFIT", quantity=1.0,
                       gross_pnl=8.0, fees_paid=0.2, net_pnl=7.8, r_multiple=1.95,
                       confluence_score=45.0, reasons=["breakout_confirmed"])


@pytest.mark.asyncio
async def test_get_open_position_returns_none_when_none_exists(pool):
    repo = PaperRepository(pool)
    assert await repo.get_open_position(_account_id(), "BTCUSDT") is None


@pytest.mark.asyncio
async def test_open_and_get_position_round_trips(pool):
    repo = PaperRepository(pool)
    account_id = _account_id()

    await repo.open_position(account_id, "BTCUSDT", _position())
    position = await repo.get_open_position(account_id, "BTCUSDT")

    assert position.side == "LONG"
    assert position.entry_price == pytest.approx(100.0)
    assert position.stop_price == pytest.approx(96.0)
    assert position.reasons == ["breakout_confirmed"]


@pytest.mark.asyncio
async def test_open_position_raises_when_one_already_open(pool):
    repo = PaperRepository(pool)
    account_id = _account_id()
    await repo.open_position(account_id, "BTCUSDT", _position())

    with pytest.raises(ValueError):
        await repo.open_position(account_id, "BTCUSDT", _position())


@pytest.mark.asyncio
async def test_close_position_removes_it(pool):
    repo = PaperRepository(pool)
    account_id = _account_id()
    await repo.open_position(account_id, "BTCUSDT", _position())

    await repo.close_position(account_id, "BTCUSDT")

    assert await repo.get_open_position(account_id, "BTCUSDT") is None


@pytest.mark.asyncio
async def test_close_position_is_a_safe_no_op_when_nothing_is_open(pool):
    repo = PaperRepository(pool)
    await repo.close_position(_account_id(), "BTCUSDT")  # must not raise


@pytest.mark.asyncio
async def test_get_open_symbols_lists_every_symbol_with_a_position(pool):
    repo = PaperRepository(pool)
    account_id = _account_id()
    await repo.open_position(account_id, "BTCUSDT", _position())
    await repo.open_position(account_id, "ETHUSDT", _position())

    symbols = await repo.get_open_symbols(account_id)

    assert set(symbols) == {"BTCUSDT", "ETHUSDT"}


@pytest.mark.asyncio
async def test_positions_are_independent_per_symbol(pool):
    repo = PaperRepository(pool)
    account_id = _account_id()
    await repo.open_position(account_id, "BTCUSDT", _position())
    await repo.open_position(account_id, "ETHUSDT", _position())

    assert await repo.get_open_position(account_id, "BTCUSDT") is not None
    assert await repo.get_open_position(account_id, "ETHUSDT") is not None

    await repo.close_position(account_id, "BTCUSDT")
    assert await repo.get_open_position(account_id, "BTCUSDT") is None
    assert await repo.get_open_position(account_id, "ETHUSDT") is not None


@pytest.mark.asyncio
async def test_record_and_fetch_trades(pool):
    repo = PaperRepository(pool)
    account_id = _account_id()
    await repo.record_trade(_trade(account_id))

    trades = await repo.fetch_trades(account_id, symbol="BTCUSDT")

    assert len(trades) == 1
    assert trades[0]["net_pnl"] == pytest.approx(7.8)
    assert trades[0]["reasons"] == ["breakout_confirmed"]


@pytest.mark.asyncio
async def test_fetch_trades_filters_by_symbol(pool):
    repo = PaperRepository(pool)
    account_id = _account_id()
    await repo.record_trade(_trade(account_id))
    other = _trade(account_id)
    other.symbol = "ETHUSDT"
    await repo.record_trade(other)

    btc_trades = await repo.fetch_trades(account_id, symbol="BTCUSDT")
    all_trades = await repo.fetch_trades(account_id)

    assert len(btc_trades) == 1
    assert len(all_trades) == 2


@pytest.mark.asyncio
async def test_cursor_round_trips_and_defaults_to_none(pool):
    repo = PaperRepository(pool)
    account_id = _account_id()

    assert await repo.get_cursor(account_id, "BTCUSDT", "1h") is None

    await repo.set_cursor(account_id, "BTCUSDT", "1h", _T0)
    assert await repo.get_cursor(account_id, "BTCUSDT", "1h") == _T0

    await repo.set_cursor(account_id, "BTCUSDT", "1h", _T1)
    assert await repo.get_cursor(account_id, "BTCUSDT", "1h") == _T1
