"""asyncpg connection pool lifecycle.

Kept deliberately thin: one function to open a pool, one to close it. Schema
is owned entirely by Alembic (see `migrations/`) - this module never issues
DDL.
"""
from __future__ import annotations

import asyncpg

from aegis.config import Settings


async def create_pool(settings: Settings) -> asyncpg.Pool:
    if not settings.database_url:
        raise ValueError("DATABASE_URL is empty - persistence is disabled, no pool to create")
    return await asyncpg.create_pool(
        dsn=settings.database_url,
        min_size=settings.db_pool_min_size,
        max_size=settings.db_pool_max_size,
    )


async def close_pool(pool: asyncpg.Pool) -> None:
    await pool.close()
