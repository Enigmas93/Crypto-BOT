"""Persistence for the Momentum Engine (Fase 14): same shape as
ShadowRepository (Phase 10) - at most one open position per (account,
symbol), an append-only closed-trade log, a per-(account, symbol,
interval) cursor. Kept as its own repository rather than generalizing
ShadowRepository: the record shapes genuinely differ (trailing_order_id/
momentum_score here, take_profit_order_id there), and forcing them through
one generic repository would blur that distinction rather than clarify it
- same reasoning already documented in shadow_repository.py.
"""
from __future__ import annotations

from datetime import datetime

import asyncpg

from aegis.momentum.models import MomentumPosition, MomentumTrade
from aegis.scanner.ranking import MomentumCandidate


class MomentumRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def get_open_position(self, account_id: str, symbol: str) -> MomentumPosition | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM momentum_positions WHERE account_id = $1 AND symbol = $2", account_id, symbol,
            )
        if row is None:
            return None
        return MomentumPosition(
            side=row["side"], entry_time=row["entry_time"], entry_price=row["entry_price"],
            quantity=row["quantity"], stop_order_id=row["stop_order_id"],
            trailing_order_id=row["trailing_order_id"], stop_price=row["stop_price"],
            risk_amount=row["risk_amount"], confluence_score=row["confluence_score"],
            momentum_score=row["momentum_score"], reasons=list(row["reasons"]),
        )

    async def get_open_symbols(self, account_id: str) -> list[str]:
        """Every symbol with a currently-open position - the engine must
        keep reconciling these even if the Scanner drops them from this
        cycle's top-N candidate list."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("SELECT symbol FROM momentum_positions WHERE account_id = $1", account_id)
        return [r["symbol"] for r in rows]

    async def open_position(self, account_id: str, symbol: str, position: MomentumPosition) -> None:
        """Raises if a position is already open for this (account, symbol)
        - MomentumTradingEngine only ever calls this after confirming none
        exists, so hitting the conflict means a real logic bug."""
        async with self._pool.acquire() as conn:
            inserted = await conn.fetchval(
                """
                INSERT INTO momentum_positions (
                    account_id, symbol, side, entry_time, entry_price, quantity,
                    stop_order_id, trailing_order_id, stop_price, risk_amount,
                    confluence_score, momentum_score, reasons
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
                ON CONFLICT (account_id, symbol) DO NOTHING
                RETURNING account_id
                """,
                account_id, symbol, position.side, position.entry_time, position.entry_price,
                position.quantity, position.stop_order_id, position.trailing_order_id,
                position.stop_price, position.risk_amount, position.confluence_score,
                position.momentum_score, position.reasons,
            )
        if inserted is None:
            raise ValueError(f"a momentum position is already open for account_id={account_id!r} symbol={symbol!r}")

    async def close_position(self, account_id: str, symbol: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM momentum_positions WHERE account_id = $1 AND symbol = $2", account_id, symbol,
            )

    async def record_trade(self, trade: MomentumTrade) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO momentum_trades (
                    account_id, symbol, side, entry_time, entry_price, exit_time, exit_price,
                    exit_reason, quantity, gross_pnl, fees_paid, net_pnl, r_multiple,
                    confluence_score, momentum_score, reasons
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16)
                """,
                trade.account_id, trade.symbol, trade.side, trade.entry_time, trade.entry_price,
                trade.exit_time, trade.exit_price, trade.exit_reason, trade.quantity, trade.gross_pnl,
                trade.fees_paid, trade.net_pnl, trade.r_multiple, trade.confluence_score,
                trade.momentum_score, trade.reasons,
            )

    async def fetch_trades(self, account_id: str, symbol: str | None = None, limit: int = 50) -> list[dict]:
        async with self._pool.acquire() as conn:
            if symbol is not None:
                rows = await conn.fetch(
                    """
                    SELECT * FROM momentum_trades WHERE account_id = $1 AND symbol = $2
                    ORDER BY closed_at DESC LIMIT $3
                    """,
                    account_id, symbol, limit,
                )
            else:
                rows = await conn.fetch(
                    "SELECT * FROM momentum_trades WHERE account_id = $1 ORDER BY closed_at DESC LIMIT $2",
                    account_id, limit,
                )
        return [dict(r) for r in rows]

    async def save_scan_results(self, candidates: list[MomentumCandidate], scanned_at: datetime) -> None:
        """Replaces the whole table every call - this is the Scanner's
        CURRENT top-N (spec: rank_by_momentum), not a history; a symbol
        that drops out of this cycle's candidates must disappear from here
        too, not linger as a stale row. Cheap at this scale (top_n=5)."""
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute("DELETE FROM momentum_scan_results")
            if candidates:
                await conn.executemany(
                    """
                    INSERT INTO momentum_scan_results (symbol, momentum_score, price_change_pct, quote_volume, scanned_at)
                    VALUES ($1,$2,$3,$4,$5)
                    """,
                    [(c.symbol, c.momentum_score, c.price_change_pct, c.quote_volume, scanned_at) for c in candidates],
                )

    async def fetch_latest_scan_results(self) -> list[dict]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT symbol, momentum_score, price_change_pct, quote_volume, scanned_at "
                "FROM momentum_scan_results ORDER BY momentum_score DESC"
            )
        return [dict(r) for r in rows]

    async def get_cursor(self, account_id: str, symbol: str, interval: str) -> datetime | None:
        async with self._pool.acquire() as conn:
            value = await conn.fetchval(
                """
                SELECT last_processed_close_time FROM momentum_trading_cursor
                WHERE account_id = $1 AND symbol = $2 AND interval = $3
                """,
                account_id, symbol, interval,
            )
        return value

    async def set_cursor(self, account_id: str, symbol: str, interval: str, close_time: datetime) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO momentum_trading_cursor (account_id, symbol, interval, last_processed_close_time)
                VALUES ($1,$2,$3,$4)
                ON CONFLICT (account_id, symbol, interval) DO UPDATE SET
                    last_processed_close_time = $4, updated_at = now()
                """,
                account_id, symbol, interval, close_time,
            )
