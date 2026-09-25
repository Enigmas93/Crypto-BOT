"""Persistence for the Shadow BingX / Momentum BingX capital split (Fase
17g) - see migration 0021's docstring for the real problem this solves
(two independent engines sharing one real exchange balance, each sizing
positions as if it alone owned the whole thing).
"""
from __future__ import annotations

import asyncpg

_ENGINES = ("shadow_bingx", "momentum_bingx")
_SUM_TOLERANCE = 1e-6


class CapitalAllocationRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def get_allocation(self, engine: str) -> float:
        """1.0 (no scaling) if unconfigured - e.g. a fresh install before
        the migration's seed rows exist, or an `engine` key nothing here
        recognizes. Never silently zeroes out a real engine's sizing."""
        async with self._pool.acquire() as conn:
            value = await conn.fetchval(
                "SELECT allocation_pct FROM capital_allocations WHERE engine = $1", engine,
            )
        return float(value) if value is not None else 1.0

    async def get_all_allocations(self) -> dict[str, float]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("SELECT engine, allocation_pct FROM capital_allocations")
        return {r["engine"]: float(r["allocation_pct"]) for r in rows}

    async def set_allocations(self, shadow_bingx_pct: float, momentum_bingx_pct: float) -> None:
        """Both at once, not one at a time - the whole point is that they
        represent a single real balance split between two engines, so a
        caller changing one without the other (leaving them summing to
        something other than 1.0) would either strand real capital unused
        or, worse, let both engines size against more than the account
        actually holds."""
        total = shadow_bingx_pct + momentum_bingx_pct
        if abs(total - 1.0) > _SUM_TOLERANCE:
            raise ValueError(f"shadow_bingx_pct + momentum_bingx_pct must sum to 1.0, got {total}")
        if shadow_bingx_pct <= 0 or momentum_bingx_pct <= 0:
            raise ValueError("both allocations must be positive - use 0.01/0.99 to functionally disable one, never 0")
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                for engine, pct in (("shadow_bingx", shadow_bingx_pct), ("momentum_bingx", momentum_bingx_pct)):
                    await conn.execute(
                        """
                        INSERT INTO capital_allocations (engine, allocation_pct, updated_at) VALUES ($1, $2, now())
                        ON CONFLICT (engine) DO UPDATE SET allocation_pct = EXCLUDED.allocation_pct, updated_at = now()
                        """,
                        engine, pct,
                    )
