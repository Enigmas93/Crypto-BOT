"""Persistence for per-strategy enable/disable toggles (Fase 17f), read
once per trading cycle by Paper/Shadow/Momentum (Binance and BingX alike)
to filter which strategy_ids get evaluated. Absence of a row means
ENABLED - this table only ever stores explicit overrides (disabled rows or
a since-re-enabled row), so a new strategy added to ALL_STRATEGY_IDS later
starts active without a data migration.
"""
from __future__ import annotations

import asyncpg


class StrategySettingsRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def get_disabled_strategy_ids(self) -> set[str]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("SELECT strategy_id FROM strategy_settings WHERE enabled = false")
        return {r["strategy_id"] for r in rows}

    async def get_all_settings(self, all_strategy_ids: tuple[str, ...]) -> dict[str, bool]:
        """Every strategy in `all_strategy_ids`, defaulting to enabled=True
        for any without an explicit row - for the dashboard's settings list."""
        disabled = await self.get_disabled_strategy_ids()
        return {sid: sid not in disabled for sid in all_strategy_ids}

    async def set_enabled(self, strategy_id: str, enabled: bool) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO strategy_settings (strategy_id, enabled, updated_at) VALUES ($1, $2, now())
                ON CONFLICT (strategy_id) DO UPDATE SET enabled = EXCLUDED.enabled, updated_at = now()
                """,
                strategy_id, enabled,
            )

    async def filter_enabled(self, strategy_ids: tuple[str, ...]) -> tuple[str, ...]:
        """What a trading engine actually calls each cycle: `config.
        strategy_ids` filtered down to whatever isn't currently disabled."""
        disabled = await self.get_disabled_strategy_ids()
        return tuple(sid for sid in strategy_ids if sid not in disabled)
