"""Integration tests for CapitalAllocationRepository against a real
TimescaleDB. Skipped automatically if the database is unreachable (see
conftest.py).
"""
from __future__ import annotations

import pytest

from aegis.db.capital_allocation_repository import CapitalAllocationRepository


@pytest.fixture
async def repo_with_restore(pool):
    """The migration seeds exactly one real, shared row pair - tests that
    mutate it always restore the original split afterward."""
    repo = CapitalAllocationRepository(pool)
    before = await repo.get_all_allocations()
    yield repo
    if "shadow_bingx" in before and "momentum_bingx" in before:
        await repo.set_allocations(before["shadow_bingx"], before["momentum_bingx"])


@pytest.mark.asyncio
async def test_migration_seeded_the_default_60_40_split(repo_with_restore):
    allocations = await repo_with_restore.get_all_allocations()
    assert allocations["shadow_bingx"] == pytest.approx(0.60)
    assert allocations["momentum_bingx"] == pytest.approx(0.40)


@pytest.mark.asyncio
async def test_get_allocation_for_an_unknown_engine_defaults_to_full_equity(repo_with_restore):
    assert await repo_with_restore.get_allocation("not_a_real_engine") == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_set_allocations_round_trips(repo_with_restore):
    await repo_with_restore.set_allocations(0.7, 0.3)

    allocations = await repo_with_restore.get_all_allocations()
    assert allocations["shadow_bingx"] == pytest.approx(0.7)
    assert allocations["momentum_bingx"] == pytest.approx(0.3)


@pytest.mark.asyncio
async def test_set_allocations_rejects_a_split_that_does_not_sum_to_one(repo_with_restore):
    with pytest.raises(ValueError):
        await repo_with_restore.set_allocations(0.5, 0.6)


@pytest.mark.asyncio
async def test_set_allocations_rejects_a_zero_share(repo_with_restore):
    with pytest.raises(ValueError):
        await repo_with_restore.set_allocations(1.0, 0.0)
