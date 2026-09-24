"""Integration tests for BingxAccountRepository against a real TimescaleDB.
Skipped automatically if the database is unreachable (see conftest.py).

Unlike every other repository's tests, this one has a single, real,
shared singleton row (the actual BingX credential the running dashboard/
trading scripts use) rather than a throwaway row under a random account_id
- every test that mutates it restores the original state in a `finally`,
using the SAME encryption key the running app is configured with
(`get_settings().credential_encryption_key`), never a throwaway test key,
so the restore step can actually decrypt whatever was there before.
"""
from __future__ import annotations

import pytest

from aegis.config import get_settings
from aegis.db.bingx_account_repository import BingxAccountRepository

pytestmark = pytest.mark.skipif(
    not get_settings().credential_encryption_key,
    reason="CREDENTIAL_ENCRYPTION_KEY not set in this environment",
)


@pytest.fixture
def repo(pool):
    return BingxAccountRepository(pool, get_settings().credential_encryption_key)


@pytest.mark.asyncio
async def test_get_settings_reflects_the_migration_seeded_singleton_row(repo):
    # Fase 17's migration always inserts exactly one row (id='singleton') -
    # this doesn't assume WHICH mode/credential state it's in (that depends
    # on this environment's .env at migration time and whatever this
    # session already changed via the dashboard), only that a row exists.
    state = await repo.get_settings()
    assert state.mode in ("demo", "live")
    assert isinstance(state.credentials_configured, bool)


@pytest.mark.asyncio
async def test_save_credentials_round_trips_through_get_decrypted_credentials(repo):
    before = await repo.get_decrypted_credentials()
    try:
        await repo.save_credentials("test-api-key-123", "test-api-secret-456")

        state = await repo.get_settings()
        assert state.credentials_configured is True
        assert await repo.get_decrypted_credentials() == ("test-api-key-123", "test-api-secret-456")
    finally:
        if before is not None:
            await repo.save_credentials(*before)


@pytest.mark.asyncio
async def test_save_credentials_does_not_change_the_current_mode(repo):
    before_state = await repo.get_settings()
    before_credentials = await repo.get_decrypted_credentials()
    try:
        await repo.set_mode("demo")
        await repo.save_credentials("another-test-key", "another-test-secret")

        assert (await repo.get_settings()).mode == "demo"
    finally:
        if before_credentials is not None:
            await repo.save_credentials(*before_credentials)
        await repo.set_mode(before_state.mode)


@pytest.mark.asyncio
async def test_set_mode_rejects_an_invalid_mode(repo):
    with pytest.raises(ValueError):
        await repo.set_mode("not-a-real-mode")


@pytest.mark.asyncio
async def test_set_mode_round_trips(repo):
    before = await repo.get_settings()
    try:
        await repo.set_mode("live")
        assert (await repo.get_settings()).mode == "live"
        await repo.set_mode("demo")
        assert (await repo.get_settings()).mode == "demo"
    finally:
        await repo.set_mode(before.mode)
