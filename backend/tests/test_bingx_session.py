"""Unit tests for aegis.execution.bingx_session.BingxSessionManager (Fase
17) - the hot-swap layer that lets run_bingx_shadow_trading.py /
run_bingx_momentum_trading.py follow a demo<->live switch made from the
dashboard without a process restart. Uses a fake account repository (no
real database or network call - BingXFuturesRestClient's constructor
doesn't touch the network, only aclose()/requests would).
"""
from __future__ import annotations

import pytest

from aegis.execution.bingx_session import BingxCredentialsNotConfigured, BingxSessionManager


class _FakeAccountRepo:
    def __init__(self, mode: str = "demo", credentials: tuple[str, str] | None = ("key", "secret")) -> None:
        self.mode = mode
        self.credentials = credentials

    class _State:
        def __init__(self, mode: str, credentials_configured: bool) -> None:
            self.mode = mode
            self.credentials_configured = credentials_configured

    async def get_settings(self):
        return self._State(self.mode, self.credentials is not None)

    async def get_decrypted_credentials(self):
        return self.credentials


@pytest.mark.asyncio
async def test_refresh_raises_when_no_credentials_configured():
    manager = BingxSessionManager(_FakeAccountRepo(credentials=None), "shadow_bingx", "test.bingx_session")
    with pytest.raises(BingxCredentialsNotConfigured):
        await manager.refresh()


@pytest.mark.asyncio
async def test_refresh_builds_a_demo_session_and_account_id():
    manager = BingxSessionManager(_FakeAccountRepo(mode="demo"), "shadow_bingx", "test.bingx_session")
    session = await manager.refresh()
    try:
        assert session.mode == "demo"
        assert session.account_id == "shadow_bingx_demo"
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_refresh_reuses_the_same_session_when_mode_is_unchanged():
    repo = _FakeAccountRepo(mode="demo")
    manager = BingxSessionManager(repo, "momentum_bingx", "test.bingx_session")
    try:
        first = await manager.refresh()
        second = await manager.refresh()
        assert first is second
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_refresh_rebuilds_the_session_when_mode_changes():
    repo = _FakeAccountRepo(mode="demo")
    manager = BingxSessionManager(repo, "shadow_bingx", "test.bingx_session")
    try:
        first = await manager.refresh()
        assert first.account_id == "shadow_bingx_demo"

        repo.mode = "live"
        second = await manager.refresh()

        assert second is not first
        assert second.mode == "live"
        assert second.account_id == "shadow_bingx_live"
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_aclose_is_safe_before_any_refresh():
    manager = BingxSessionManager(_FakeAccountRepo(), "shadow_bingx", "test.bingx_session")
    await manager.aclose()  # must not raise
