"""Tracks how far each open position went in our favor / against us (from
the exchange mark price on every poll) and stamps the closed trade with
MFE/MAE in R. Answers "was this loser ever green?" with real data instead
of memory. R is measured against the INITIAL stop distance."""
from __future__ import annotations

import asyncpg

_TABLES = {
    "shadow": ("shadow_positions", "shadow_trades"),
    "momentum": ("momentum_positions", "momentum_trades"),
}


def excursion_r(side: str, entry: float, stop: float, best: float | None, worst: float | None,
                exit_price: float) -> tuple[float | None, float | None]:
    risk = abs(entry - stop)
    if risk <= 0:
        return None, None
    if side == "LONG":
        hi = max(p for p in (best, exit_price, entry) if p is not None)
        lo = min(p for p in (worst, exit_price, entry) if p is not None)
        return round((hi - entry) / risk, 3), round((entry - lo) / risk, 3)
    lo = min(p for p in (best, exit_price, entry) if p is not None)
    hi = max(p for p in (worst, exit_price, entry) if p is not None)
    return round((entry - lo) / risk, 3), round((hi - entry) / risk, 3)


class ExcursionRepository:
    def __init__(self, pool: asyncpg.Pool, engine: str) -> None:
        self._pool = pool
        self._positions, self._trades = _TABLES[engine]

    async def track(self, account_id: str, symbol: str, side: str, mark_price: float) -> None:
        if not mark_price or mark_price <= 0:
            return
        better, worse = ("GREATEST", "LEAST") if side == "LONG" else ("LEAST", "GREATEST")
        async with self._pool.acquire() as conn:
            await conn.execute(
                f"""
                UPDATE {self._positions} SET
                    best_price = {better}(COALESCE(best_price, entry_price), $3),
                    worst_price = {worse}(COALESCE(worst_price, entry_price), $3)
                WHERE account_id = $1 AND symbol = $2
                """,
                account_id, symbol, mark_price,
            )

    async def compute(self, account_id: str, symbol: str, exit_price: float) -> tuple[float | None, float | None]:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT side, entry_price, stop_price, best_price, worst_price FROM {self._positions} "
                "WHERE account_id = $1 AND symbol = $2",
                account_id, symbol,
            )
        if row is None:
            return None, None
        return excursion_r(row["side"], row["entry_price"], row["stop_price"], row["best_price"],
                           row["worst_price"], exit_price)

    async def stamp_last_trade(self, account_id: str, symbol: str, mfe_r: float | None, mae_r: float | None) -> None:
        if mfe_r is None:
            return
        async with self._pool.acquire() as conn:
            await conn.execute(
                f"""
                UPDATE {self._trades} SET mfe_r = $3, mae_r = $4
                WHERE id = (SELECT id FROM {self._trades} WHERE account_id = $1 AND symbol = $2
                            ORDER BY id DESC LIMIT 1)
                """,
                account_id, symbol, mfe_r, mae_r,
            )
