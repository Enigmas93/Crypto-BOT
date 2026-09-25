"""Integration tests for StrategySettingsRepository against a real
TimescaleDB. Skipped automatically if the database is unreachable (see
conftest.py).
"""
from __future__ import annotations

import uuid

import pytest

from aegis.db.strategy_settings_repository import StrategySettingsRepository


def _strategy_id() -> str:
    return f"TEST_STRATEGY_{uuid.uuid4().hex[:8]}"


@pytest.mark.asyncio
async def test_unknown_strategy_defaults_to_enabled(pool):
    repo = StrategySettingsRepository(pool)
    disabled = await repo.get_disabled_strategy_ids()
    assert _strategy_id() not in disabled


@pytest.mark.asyncio
async def test_set_enabled_false_then_true_round_trips(pool):
    repo = StrategySettingsRepository(pool)
    strategy_id = _strategy_id()

    await repo.set_enabled(strategy_id, False)
    assert strategy_id in await repo.get_disabled_strategy_ids()

    await repo.set_enabled(strategy_id, True)
    assert strategy_id not in await repo.get_disabled_strategy_ids()


@pytest.mark.asyncio
async def test_get_all_settings_defaults_missing_strategies_to_enabled(pool):
    repo = StrategySettingsRepository(pool)
    a, b = _strategy_id(), _strategy_id()
    await repo.set_enabled(a, False)

    settings = await repo.get_all_settings((a, b))

    assert settings == {a: False, b: True}


@pytest.mark.asyncio
async def test_filter_enabled_excludes_disabled_strategies(pool):
    repo = StrategySettingsRepository(pool)
    a, b, c = _strategy_id(), _strategy_id(), _strategy_id()
    await repo.set_enabled(b, False)

    result = await repo.filter_enabled((a, b, c))

    assert result == (a, c)
