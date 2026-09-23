import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import asyncpg
import pytest

from aegis.config import get_settings


@pytest.fixture
async def pool():
    """Shared fixture for every DB integration test. Skips (never fails)
    when the database is unreachable or the schema hasn't been migrated,
    so `pytest -q` stays green offline."""
    settings = get_settings()
    try:
        p = await asyncpg.create_pool(dsn=settings.database_url, min_size=1, max_size=2)
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(f"database not reachable at {settings.database_url}: {exc}")
        return
    try:
        async with p.acquire() as conn:
            await conn.fetchval("SELECT 1")
    except asyncpg.PostgresError as exc:
        await p.close()
        pytest.skip(f"database reachable but schema missing (run migrations): {exc}")
        return
    yield p
    await p.close()


# Every integration test generates its own unique account_id/symbol/
# series_id/source_id under a "test"/"TEST" prefix (e.g. `testshadow_<hex>`)
# specifically so it can never collide with real data - but nothing ever
# deleted those rows afterward, so `pytest -q` silently accumulated
# hundreds of debris rows in the real, shared database on every run (found
# 2026-09-23: ~1700 rows across 19 tables, some dating back to early
# phases). A row starting with that prefix is never real data (no real
# account_id/symbol/series_id/source_id in this project starts with
# "test"), so sweeping it after every session is unconditionally safe -
# this replaces what used to be an ad-hoc manual cleanup.
_TEST_PREFIX_TABLES = [
    # (table, column) - children before the parents they have a real FK to.
    ("backtest_trades", None),  # via backtest_runs.id, handled specially below
    ("backtest_runs", "symbol"),
    ("shadow_positions", "account_id"), ("shadow_trades", "account_id"), ("shadow_trading_cursor", "account_id"),
    ("paper_positions", "account_id"), ("paper_trades", "account_id"), ("paper_trading_cursor", "account_id"),
    ("momentum_positions", "account_id"), ("momentum_trades", "account_id"), ("momentum_trading_cursor", "account_id"),
    ("risk_account_state", "account_id"), ("kill_switch_state", "account_id"),
    ("kill_switch_events", "account_id"), ("risk_events", "account_id"),
    ("news_entities", "source_id"), ("news", "source_id"), ("news_sources", "source_id"),
    ("macro_observations", "series_id"), ("macro_snapshots", "series_id"), ("macro_series", "series_id"),
    ("assets", "symbol"), ("book_ticker", "symbol"), ("funding_rates", "symbol"),
    ("open_interest", "symbol"), ("long_short_ratios", "symbol"), ("candles", "symbol"),
    ("trades", "symbol"), ("market_features", "symbol"), ("news_asset_status", "asset"),
    ("news_asset_status_history", "asset"), ("liquidations", "symbol"),
    ("coinmarketcal_events", "event_id"),
]


async def _sweep_test_debris() -> None:
    settings = get_settings()
    try:
        conn = await asyncpg.connect(dsn=settings.database_url)
    except (OSError, asyncpg.PostgresError):
        return  # same "stay green offline" policy as the pool fixture
    try:
        async with conn.transaction():
            await conn.execute(
                "DELETE FROM backtest_trades WHERE run_id IN "
                "(SELECT id FROM backtest_runs WHERE symbol ILIKE 'test%')"
            )
            for table, column in _TEST_PREFIX_TABLES:
                if column is None:
                    continue
                await conn.execute(f"DELETE FROM {table} WHERE {column} ILIKE 'test%'")
    except asyncpg.PostgresError:
        pass  # best-effort - a cleanup failure must never fail the test run
    finally:
        await conn.close()


def pytest_sessionfinish(session, exitstatus) -> None:
    asyncio.run(_sweep_test_debris())
