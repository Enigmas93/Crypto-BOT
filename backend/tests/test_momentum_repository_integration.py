"""Integration tests for MomentumRepository against a real TimescaleDB.
Skipped automatically if the database is unreachable (see conftest.py).
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from aegis.db.momentum_repository import MomentumRepository
from aegis.momentum.models import MomentumPosition, MomentumTrade
from aegis.scanner.ranking import MomentumCandidate

_T0 = datetime(2026, 1, 1, tzinfo=UTC)
_T1 = datetime(2026, 1, 1, 1, tzinfo=UTC)


def _account_id() -> str:
    return f"testmomentum_{uuid.uuid4().hex[:8]}"


def _position() -> MomentumPosition:
    return MomentumPosition(side="LONG", entry_time=_T0, entry_price=100.0, quantity=1.0,
                             stop_order_id=101, trailing_order_id=102, stop_price=96.0,
                             risk_amount=4.0, confluence_score=45.0, momentum_score=18.5,
                             reasons=["breakout_confirmed"])


def _trade(account_id: str) -> MomentumTrade:
    return MomentumTrade(account_id=account_id, symbol="SOLUSDT", side="LONG", entry_time=_T0, entry_price=100.0,
                          exit_time=_T1, exit_price=120.0, exit_reason="TRAILING_STOP", quantity=1.0,
                          gross_pnl=20.0, fees_paid=0.2, net_pnl=19.8, r_multiple=4.95,
                          confluence_score=45.0, momentum_score=18.5, reasons=["breakout_confirmed"])


@pytest.mark.asyncio
async def test_get_open_position_returns_none_when_none_exists(pool):
    repo = MomentumRepository(pool)
    assert await repo.get_open_position(_account_id(), "SOLUSDT") is None


@pytest.mark.asyncio
async def test_open_and_get_position_round_trips_with_trailing_order_id(pool):
    repo = MomentumRepository(pool)
    account_id = _account_id()

    await repo.open_position(account_id, "SOLUSDT", _position())
    position = await repo.get_open_position(account_id, "SOLUSDT")

    assert position.side == "LONG"
    assert position.stop_order_id == 101
    assert position.trailing_order_id == 102
    assert position.momentum_score == pytest.approx(18.5)
    assert position.reasons == ["breakout_confirmed"]


@pytest.mark.asyncio
async def test_open_position_raises_when_one_already_open(pool):
    repo = MomentumRepository(pool)
    account_id = _account_id()
    await repo.open_position(account_id, "SOLUSDT", _position())

    with pytest.raises(ValueError):
        await repo.open_position(account_id, "SOLUSDT", _position())


@pytest.mark.asyncio
async def test_close_position_removes_it(pool):
    repo = MomentumRepository(pool)
    account_id = _account_id()
    await repo.open_position(account_id, "SOLUSDT", _position())

    await repo.close_position(account_id, "SOLUSDT")

    assert await repo.get_open_position(account_id, "SOLUSDT") is None


@pytest.mark.asyncio
async def test_get_open_symbols_lists_every_symbol_with_a_position(pool):
    repo = MomentumRepository(pool)
    account_id = _account_id()
    await repo.open_position(account_id, "SOLUSDT", _position())
    await repo.open_position(account_id, "DOGEUSDT", _position())

    symbols = await repo.get_open_symbols(account_id)

    assert set(symbols) == {"SOLUSDT", "DOGEUSDT"}


@pytest.mark.asyncio
async def test_record_and_fetch_trades(pool):
    repo = MomentumRepository(pool)
    account_id = _account_id()
    await repo.record_trade(_trade(account_id))

    trades = await repo.fetch_trades(account_id, symbol="SOLUSDT")

    assert len(trades) == 1
    assert trades[0]["net_pnl"] == pytest.approx(19.8)
    assert trades[0]["momentum_score"] == pytest.approx(18.5)
    assert trades[0]["exit_reason"] == "TRAILING_STOP"


@pytest.mark.asyncio
async def test_cursor_round_trips_and_defaults_to_none(pool):
    repo = MomentumRepository(pool)
    account_id = _account_id()

    assert await repo.get_cursor(account_id, "SOLUSDT", "15m") is None

    await repo.set_cursor(account_id, "SOLUSDT", "15m", _T0)
    assert await repo.get_cursor(account_id, "SOLUSDT", "15m") == _T0

    await repo.set_cursor(account_id, "SOLUSDT", "15m", _T1)
    assert await repo.get_cursor(account_id, "SOLUSDT", "15m") == _T1


@pytest.mark.asyncio
async def test_scan_results_round_trip_and_fully_replace_on_each_save(pool):
    # Unlike every other table this repository touches, momentum_scan_results
    # has no account_id/test-prefix namespace to isolate into - it's the
    # single, global "current top-N" the real run_momentum_trading.py
    # process also writes to every ~15 minutes. save_scan_results() deletes
    # the whole table before inserting, so this test snapshots whatever is
    # really there first and restores it afterward, rather than either
    # skipping coverage or risking a lingering clobber of the live
    # dashboard's data between now and the next real scan cycle.
    repo = MomentumRepository(pool)
    real_snapshot = await repo.fetch_latest_scan_results()

    try:
        candidates = [
            MomentumCandidate(symbol="TESTABCUSDT", price_change_pct=42.0, quote_volume=6e8, momentum_score=42.0),
            MomentumCandidate(symbol="TESTXYZUSDT", price_change_pct=-13.5, quote_volume=7e8, momentum_score=13.5),
        ]
        await repo.save_scan_results(candidates, scanned_at=_T0)

        results = await repo.fetch_latest_scan_results()
        assert len(results) == 2
        by_symbol = {r["symbol"]: r for r in results}
        assert by_symbol["TESTABCUSDT"]["momentum_score"] == pytest.approx(42.0)
        assert by_symbol["TESTXYZUSDT"]["price_change_pct"] == pytest.approx(-13.5)

        # A second save must fully REPLACE, not accumulate.
        await repo.save_scan_results(
            [MomentumCandidate(symbol="TESTONLYUSDT", price_change_pct=5.0, quote_volume=5e8, momentum_score=5.0)],
            scanned_at=_T1,
        )
        results = await repo.fetch_latest_scan_results()
        assert [r["symbol"] for r in results] == ["TESTONLYUSDT"]
    finally:
        await repo.save_scan_results(
            [
                MomentumCandidate(symbol=r["symbol"], price_change_pct=r["price_change_pct"],
                                   quote_volume=r["quote_volume"], momentum_score=r["momentum_score"])
                for r in real_snapshot
            ],
            scanned_at=real_snapshot[0]["scanned_at"] if real_snapshot else _T0,
        )
