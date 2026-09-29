"""Persistence for the trend engine's paper book (Fase 24, migration 0025)."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime

import asyncpg

from aegis.trend.book import Book, Fill, Position


@dataclass(slots=True)
class TrendState:
    book: Book
    last_rebalance_bar: datetime | None
    last_funding_ms: int
    opened_at: dict[str, datetime] = field(default_factory=dict)


class TrendRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def load(self, account_id: str, starting_equity: float, now_ms: int) -> TrendState:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM trend_account WHERE account_id = $1", account_id)
            if row is None:
                # Funding watermark starts NOW: history before the book existed is not ours to pay.
                await conn.execute(
                    "INSERT INTO trend_account (account_id, starting_equity, cash, equity, last_funding_ms) "
                    "VALUES ($1, $2, $2, $2, $3)", account_id, starting_equity, now_ms,
                )
                return TrendState(Book(cash=starting_equity), None, now_ms)
            pos_rows = await conn.fetch("SELECT * FROM trend_positions WHERE account_id = $1", account_id)
        book = Book(cash=row["cash"], positions={r["symbol"]: Position(r["qty"], r["entry_price"]) for r in pos_rows})
        return TrendState(book, row["last_rebalance_bar"], row["last_funding_ms"],
                          {r["symbol"]: r["opened_at"] for r in pos_rows if r["opened_at"]})

    async def save(self, account_id: str, state: TrendState, prices: dict[str, float], now: datetime,
                   fills: list[Fill] = (), funding: list[tuple[str, datetime, float]] = (),
                   signal: dict | None = None) -> None:
        equity = state.book.equity(prices)
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "UPDATE trend_account SET cash = $2, equity = $3, last_rebalance_bar = $4, last_funding_ms = $5, "
                "last_signal = COALESCE($6::jsonb, last_signal), updated_at = $7 WHERE account_id = $1",
                account_id, state.book.cash, equity, state.last_rebalance_bar, state.last_funding_ms,
                json.dumps(signal) if signal is not None else None, now,
            )
            for symbol, pos in state.book.positions.items():
                if pos.qty == 0:
                    await conn.execute("DELETE FROM trend_positions WHERE account_id = $1 AND symbol = $2",
                                       account_id, symbol)
                    continue
                await conn.execute(
                    """
                    INSERT INTO trend_positions (account_id, symbol, qty, entry_price, mark_price, opened_at)
                    VALUES ($1, $2, $3, $4, $5, $6)
                    ON CONFLICT (account_id, symbol) DO UPDATE SET qty = EXCLUDED.qty,
                        entry_price = EXCLUDED.entry_price, mark_price = EXCLUDED.mark_price,
                        opened_at = EXCLUDED.opened_at
                    """,
                    account_id, symbol, pos.qty, pos.entry_price, prices.get(symbol),
                    state.opened_at.get(symbol, now),
                )
            for f in fills:
                await conn.execute(
                    "INSERT INTO trend_ledger (account_id, ts, symbol, kind, qty, price, fee, realized_pnl, "
                    "side_before, side_after) VALUES ($1, $2, $3, 'TRADE', $4, $5, $6, $7, $8, $9)",
                    account_id, now, f.symbol, f.qty, f.price, f.fee, f.realized_pnl, f.side_before, f.side_after,
                )
            for symbol, ts, amount in funding:
                await conn.execute(
                    "INSERT INTO trend_ledger (account_id, ts, symbol, kind, funding) VALUES ($1, $2, $3, 'FUNDING', $4)",
                    account_id, ts, symbol, amount,
                )
            hour = now.replace(minute=0, second=0, microsecond=0)
            await conn.execute(
                "INSERT INTO trend_equity (account_id, ts, equity) VALUES ($1, $2, $3) "
                "ON CONFLICT (account_id, ts) DO UPDATE SET equity = EXCLUDED.equity",
                account_id, hour, equity,
            )

    async def position_net_pnl(self, account_id: str, symbol: str, since: datetime) -> float:
        """Realized PnL - fees + funding over one position's lifetime (for the close notification)."""
        async with self._pool.acquire() as conn:
            value = await conn.fetchval(
                "SELECT COALESCE(sum(realized_pnl - fee + funding), 0) FROM trend_ledger "
                "WHERE account_id = $1 AND symbol = $2 AND ts >= $3", account_id, symbol, since,
            )
        return float(value or 0.0)

    async def summary(self, account_id: str) -> dict | None:
        async with self._pool.acquire() as conn:
            acc = await conn.fetchrow("SELECT * FROM trend_account WHERE account_id = $1", account_id)
            if acc is None:
                return None
            positions = await conn.fetch("SELECT * FROM trend_positions WHERE account_id = $1 ORDER BY symbol",
                                         account_id)
            ledger = await conn.fetch(
                "SELECT * FROM trend_ledger WHERE account_id = $1 ORDER BY ts DESC, id DESC LIMIT 60", account_id)
            equity = await conn.fetch(
                "SELECT ts, equity FROM trend_equity WHERE account_id = $1 AND ts > now() - interval '400 days' "
                "ORDER BY ts", account_id)
            totals = await conn.fetchrow(
                "SELECT COALESCE(sum(fee), 0) AS fees, COALESCE(sum(funding), 0) AS funding, "
                "COALESCE(sum(realized_pnl), 0) AS realized FROM trend_ledger WHERE account_id = $1", account_id)
        return {
            "account_id": account_id,
            "starting_equity": acc["starting_equity"], "cash": acc["cash"], "equity": acc["equity"],
            "last_rebalance_bar": acc["last_rebalance_bar"].isoformat() if acc["last_rebalance_bar"] else None,
            "updated_at": acc["updated_at"].isoformat(), "created_at": acc["created_at"].isoformat(),
            "signal": json.loads(acc["last_signal"]) if acc["last_signal"] else None,
            "positions": [
                {"symbol": r["symbol"], "qty": r["qty"], "entry_price": r["entry_price"], "mark_price": r["mark_price"],
                 "side": "LONG" if r["qty"] > 0 else "SHORT",
                 "notional": abs(r["qty"]) * (r["mark_price"] or r["entry_price"]),
                 "unrealized_pnl": r["qty"] * ((r["mark_price"] or r["entry_price"]) - r["entry_price"]),
                 "opened_at": r["opened_at"].isoformat() if r["opened_at"] else None}
                for r in positions
            ],
            "ledger": [
                {"ts": r["ts"].isoformat(), "symbol": r["symbol"], "kind": r["kind"], "qty": r["qty"],
                 "price": r["price"], "fee": r["fee"], "realized_pnl": r["realized_pnl"], "funding": r["funding"],
                 "side_before": r["side_before"], "side_after": r["side_after"]}
                for r in ledger
            ],
            "equity_curve": [{"ts": r["ts"].isoformat(), "equity": r["equity"]} for r in equity],
            "totals": {"fees": totals["fees"], "funding": totals["funding"], "realized": totals["realized"]},
        }
