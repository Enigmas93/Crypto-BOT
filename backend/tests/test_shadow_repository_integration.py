"""Integration tests for ShadowRepository against a real TimescaleDB.
Skipped automatically if the database is unreachable (see conftest.py).
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from aegis.db.shadow_repository import ShadowRepository
from aegis.shadow.models import ShadowPosition, ShadowTrade

_T0 = datetime(2026, 1, 1, tzinfo=UTC)
_T1 = datetime(2026, 1, 1, 1, tzinfo=UTC)


def _account_id() -> str:
    return f"testshadow_{uuid.uuid4().hex[:8]}"


def _position() -> ShadowPosition:
    return ShadowPosition(side="LONG", entry_time=_T0, entry_price=100.0, quantity=1.0,
                           stop_order_id=101, take_profit_order_id=102, stop_price=96.0,
                           take_profit_price=108.0, risk_amount=4.0, confluence_score=45.0,
                           reasons=["breakout_confirmed"])


def _trade(account_id: str) -> ShadowTrade:
    return ShadowTrade(account_id=account_id, symbol="BTCUSDT", side="LONG", entry_time=_T0, entry_price=100.0,
                        exit_time=_T1, exit_price=108.0, exit_reason="TAKE_PROFIT", quantity=1.0,
                        gross_pnl=8.0, fees_paid=0.2, net_pnl=7.8, r_multiple=1.95,
                        confluence_score=45.0, reasons=["breakout_confirmed"])


@pytest.mark.asyncio
async def test_get_open_position_returns_none_when_none_exists(pool):
    repo = ShadowRepository(pool)
    assert await repo.get_open_position(_account_id(), "BTCUSDT") is None


@pytest.mark.asyncio
async def test_open_and_get_position_round_trips_with_order_ids(pool):
    repo = ShadowRepository(pool)
    account_id = _account_id()

    await repo.open_position(account_id, "BTCUSDT", _position())
    position = await repo.get_open_position(account_id, "BTCUSDT")

    assert position.side == "LONG"
    assert position.stop_order_id == 101
    assert position.take_profit_order_id == 102
    assert position.reasons == ["breakout_confirmed"]


@pytest.mark.asyncio
async def test_open_position_raises_when_one_already_open(pool):
    repo = ShadowRepository(pool)
    account_id = _account_id()
    await repo.open_position(account_id, "BTCUSDT", _position())

    with pytest.raises(ValueError):
        await repo.open_position(account_id, "BTCUSDT", _position())


@pytest.mark.asyncio
async def test_close_position_removes_it(pool):
    repo = ShadowRepository(pool)
    account_id = _account_id()
    await repo.open_position(account_id, "BTCUSDT", _position())

    await repo.close_position(account_id, "BTCUSDT")

    assert await repo.get_open_position(account_id, "BTCUSDT") is None


@pytest.mark.asyncio
async def test_record_and_fetch_trades(pool):
    repo = ShadowRepository(pool)
    account_id = _account_id()
    await repo.record_trade(_trade(account_id))

    trades = await repo.fetch_trades(account_id, symbol="BTCUSDT")

    assert len(trades) == 1
    assert trades[0]["net_pnl"] == pytest.approx(7.8)
    assert trades[0]["exit_reason"] == "TAKE_PROFIT"


@pytest.mark.asyncio
async def test_cursor_round_trips_and_defaults_to_none(pool):
    repo = ShadowRepository(pool)
    account_id = _account_id()

    assert await repo.get_cursor(account_id, "BTCUSDT", "1h") is None

    await repo.set_cursor(account_id, "BTCUSDT", "1h", _T0)
    assert await repo.get_cursor(account_id, "BTCUSDT", "1h") == _T0

    await repo.set_cursor(account_id, "BTCUSDT", "1h", _T1)
    assert await repo.get_cursor(account_id, "BTCUSDT", "1h") == _T1
