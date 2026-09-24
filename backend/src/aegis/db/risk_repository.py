"""Persistence for the Risk Engine (Phase 7): one account-state row per
account, updated atomically by SQL (never read-modify-write in Python -
`record_trade_outcome` is a single `UPDATE ... RETURNING`), plus an
append-only audit log of every risk decision (spec section 85).
"""
from __future__ import annotations

import asyncpg

from aegis.risk.service import AccountState, RiskDecision


class RiskRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    @staticmethod
    def _to_account_state(row: asyncpg.Record) -> AccountState:
        return AccountState(
            equity=row["equity"],
            peak_equity=row["peak_equity"],
            daily_starting_equity=row["daily_starting_equity"],
            daily_realized_pnl=row["daily_realized_pnl"],
            consecutive_losses=row["consecutive_losses"],
            open_positions_count=row["open_positions_count"],
            correlated_exposure_pct=row["correlated_exposure_pct"],
        )

    async def get_account_state(self, account_id: str = "default") -> AccountState | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM risk_account_state WHERE account_id = $1", account_id)
        return self._to_account_state(row) if row else None

    async def get_updated_at(self, account_id: str):
        """For the daily-reset scheduler ONLY (`scripts/run_daily_reset.py`)
        - lets it tell "already reset today" (something touched this row
        today, possibly `reset_daily` itself) apart from "stale since a
        previous day" without a dedicated tracking column. Returns None if
        the account doesn't exist yet."""
        async with self._pool.acquire() as conn:
            return await conn.fetchval("SELECT updated_at FROM risk_account_state WHERE account_id = $1", account_id)

    async def initialize_account_state(self, account_id: str, starting_equity: float) -> AccountState:
        """No-op if the account already exists - this never resets a live
        account's history just because startup code calls it again."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO risk_account_state (account_id, equity, peak_equity, daily_starting_equity)
                VALUES ($1, $2, $2, $2)
                ON CONFLICT (account_id) DO NOTHING
                """,
                account_id, starting_equity,
            )
        return await self.get_account_state(account_id)

    async def record_trade_outcome(self, account_id: str, pnl_amount: float) -> AccountState:
        """Atomic: equity/peak/daily-pnl/streak all move together in one
        UPDATE, never a Python read-then-write that could race."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE risk_account_state SET
                    equity = equity + $2,
                    peak_equity = GREATEST(peak_equity, equity + $2),
                    daily_realized_pnl = daily_realized_pnl + $2,
                    consecutive_losses = CASE WHEN $2 < 0 THEN consecutive_losses + 1 ELSE 0 END,
                    updated_at = now()
                WHERE account_id = $1
                RETURNING *
                """,
                account_id, pnl_amount,
            )
        if row is None:
            raise ValueError(f"no risk_account_state row for account_id={account_id!r} - initialize it first")
        return self._to_account_state(row)

    async def reset_daily(self, account_id: str = "default") -> AccountState:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE risk_account_state SET
                    daily_starting_equity = equity, daily_realized_pnl = 0, updated_at = now()
                WHERE account_id = $1
                RETURNING *
                """,
                account_id,
            )
        if row is None:
            raise ValueError(f"no risk_account_state row for account_id={account_id!r} - initialize it first")
        return self._to_account_state(row)

    async def set_exposure(
        self, account_id: str, open_positions_count: int, correlated_exposure_pct: float = 0.0,
    ) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE risk_account_state SET
                    open_positions_count = $2, correlated_exposure_pct = $3, updated_at = now()
                WHERE account_id = $1
                """,
                account_id, open_positions_count, correlated_exposure_pct,
            )

    async def insert_risk_event(
        self, account_id: str, symbol: str, side: str, decision: RiskDecision, strategy_id: str | None = None,
    ) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO risk_events (account_id, symbol, side, strategy_id, decision, reasons,
                                          drawdown_state, effective_risk_per_trade, position_quantity,
                                          position_notional, net_r_multiple)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
                """,
                account_id, symbol, side, strategy_id, decision.decision, decision.reasons,
                decision.drawdown_state, decision.effective_risk_per_trade,
                decision.position_size.quantity, decision.position_size.notional, decision.net_r_multiple,
            )

    async def fetch_recent_events(self, account_id: str = "default", limit: int = 20) -> list[dict]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT occurred_at, symbol, side, decision, reasons, drawdown_state,
                       position_quantity, position_notional, net_r_multiple
                FROM risk_events WHERE account_id = $1
                ORDER BY occurred_at DESC LIMIT $2
                """,
                account_id, limit,
            )
        return [dict(r) for r in rows]
