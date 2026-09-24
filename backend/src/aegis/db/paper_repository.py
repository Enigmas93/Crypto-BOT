"""Persistence for Paper Trading (Phase 9): at most one open position per
(account, symbol), an append-only closed-trade log, and a per-(account,
symbol,interval) cursor so a poller calling `PaperTradingEngine.run_once`
every N seconds never re-processes the same already-closed candle twice.
"""
from __future__ import annotations

from datetime import datetime

import asyncpg

from aegis.execution.models import OpenPosition
from aegis.paper.models import PaperTrade


class PaperRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def get_open_position(self, account_id: str, symbol: str) -> OpenPosition | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM paper_positions WHERE account_id = $1 AND symbol = $2", account_id, symbol,
            )
        if row is None:
            return None
        return OpenPosition(
            side=row["side"], entry_time=row["entry_time"], entry_price=row["entry_price"],
            stop_price=row["stop_price"], take_profit_price=row["take_profit_price"],
            quantity=row["quantity"], risk_amount=row["risk_amount"],
            confluence_score=row["confluence_score"], reasons=list(row["reasons"]),
        )

    async def get_open_symbols(self, account_id: str) -> list[str]:
        """Every symbol with a currently-open position for this account -
        used to keep `risk_account_state.open_positions_count` accurate
        (RiskEngine's `max_open_positions` gate reads that field directly,
        spec section 142)."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("SELECT symbol FROM paper_positions WHERE account_id = $1", account_id)
        return [r["symbol"] for r in rows]

    async def open_position(self, account_id: str, symbol: str, position: OpenPosition) -> None:
        """Raises if a position is already open for this (account, symbol)
        - PaperTradingEngine only ever calls this after confirming none
        exists, so hitting the conflict means a real logic bug, not a
        condition to silently paper over."""
        async with self._pool.acquire() as conn:
            inserted = await conn.fetchval(
                """
                INSERT INTO paper_positions (
                    account_id, symbol, side, entry_time, entry_price, stop_price,
                    take_profit_price, quantity, risk_amount, confluence_score, reasons
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
                ON CONFLICT (account_id, symbol) DO NOTHING
                RETURNING account_id
                """,
                account_id, symbol, position.side, position.entry_time, position.entry_price,
                position.stop_price, position.take_profit_price, position.quantity,
                position.risk_amount, position.confluence_score, position.reasons,
            )
        if inserted is None:
            raise ValueError(f"a paper position is already open for account_id={account_id!r} symbol={symbol!r}")

    async def close_position(self, account_id: str, symbol: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM paper_positions WHERE account_id = $1 AND symbol = $2", account_id, symbol,
            )

    async def record_trade(self, trade: PaperTrade) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO paper_trades (
                    account_id, symbol, side, entry_time, entry_price, exit_time, exit_price,
                    exit_reason, quantity, gross_pnl, fees_paid, net_pnl, r_multiple,
                    confluence_score, reasons
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)
                """,
                trade.account_id, trade.symbol, trade.side, trade.entry_time, trade.entry_price,
                trade.exit_time, trade.exit_price, trade.exit_reason, trade.quantity, trade.gross_pnl,
                trade.fees_paid, trade.net_pnl, trade.r_multiple, trade.confluence_score, trade.reasons,
            )

    async def fetch_trades(self, account_id: str, symbol: str | None = None, limit: int = 50) -> list[dict]:
        async with self._pool.acquire() as conn:
            if symbol is not None:
                rows = await conn.fetch(
                    """
                    SELECT * FROM paper_trades WHERE account_id = $1 AND symbol = $2
                    ORDER BY closed_at DESC LIMIT $3
                    """,
                    account_id, symbol, limit,
                )
            else:
                rows = await conn.fetch(
                    "SELECT * FROM paper_trades WHERE account_id = $1 ORDER BY closed_at DESC LIMIT $2",
                    account_id, limit,
                )
        return [dict(r) for r in rows]

    async def get_cursor(self, account_id: str, symbol: str, interval: str) -> datetime | None:
        async with self._pool.acquire() as conn:
            value = await conn.fetchval(
                """
                SELECT last_processed_close_time FROM paper_trading_cursor
                WHERE account_id = $1 AND symbol = $2 AND interval = $3
                """,
                account_id, symbol, interval,
            )
        return value

    async def set_cursor(self, account_id: str, symbol: str, interval: str, close_time: datetime) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO paper_trading_cursor (account_id, symbol, interval, last_processed_close_time)
                VALUES ($1,$2,$3,$4)
                ON CONFLICT (account_id, symbol, interval) DO UPDATE SET
                    last_processed_close_time = $4, updated_at = now()
                """,
                account_id, symbol, interval, close_time,
            )
