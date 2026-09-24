"""Integration tests for the Phase 11 Dashboard API against a real
TimescaleDB. Skipped automatically if the database is unreachable (see
conftest.py).

Unlike other repositories, the tracked accounts ("paper", "shadow",
"shadow_bingx", "momentum", "momentum_bingx") are fixed by design - the
dashboard has exactly five real accounts, not an arbitrary set a test can
spin up under a random unique id the way risk/kill-switch/paper/shadow/
momentum repository tests do. These
tests therefore assert response SHAPE and status codes rather than exact
values (the real account state changes as other work in this session
runs), and only exercise write-path validation (bad input, not-currently-
triggered) that never mutates real account state.
"""
from __future__ import annotations

import httpx
import pytest

from aegis.api.app import create_app
from aegis.config import get_settings
from aegis.providers.bingx.rest_client import BingXFuturesRestClient
from aegis.providers.binance.rest_client import BinanceFuturesRestClient


@pytest.fixture
async def client(pool):
    settings = get_settings()
    app = create_app()
    app.state.pool = pool
    app.state.settings = settings
    # Not exercised via the real lifespan here (ASGITransport doesn't run
    # it) - same manual wiring as pool/settings above. Credentials may be
    # empty in this environment; every call on these clients already
    # degrades to "no live enrichment" rather than raising (see
    # routes._enrich_with_live_state), so this is safe either way.
    app.state.binance_rest = BinanceFuturesRestClient(
        testnet=settings.binance_testnet, api_key=settings.binance_api_key, api_secret=settings.binance_api_secret,
    )
    app.state.bingx_rest = BingXFuturesRestClient(
        testnet=settings.bingx_testnet, api_key=settings.bingx_api_key, api_secret=settings.bingx_api_secret,
    )
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
    finally:
        await app.state.binance_rest.aclose()
        await app.state.bingx_rest.aclose()


@pytest.mark.asyncio
async def test_overview_returns_both_tracked_accounts(client):
    resp = await client.get("/api/overview")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body["accounts"].keys()) == {"paper", "shadow", "shadow_bingx", "momentum", "momentum_bingx"}
    for account in body["accounts"].values():
        assert "initialized" in account


@pytest.mark.asyncio
async def test_positions_returns_paper_shadow_and_momentum_lists(client):
    resp = await client.get("/api/positions")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body["paper"], list)
    assert isinstance(body["shadow"], list)
    assert isinstance(body["shadow_bingx"], list)
    assert isinstance(body["momentum"], list)
    assert isinstance(body["momentum_bingx"], list)
    # Momentum has no fixed take_profit_price - it exits via trailing stop
    for position in body["momentum"] + body["momentum_bingx"]:
        assert "take_profit_price" not in position
        assert "momentum_score" in position
    # Every real-exchange account's positions carry live enrichment fields
    # (never for "paper", which has no real exchange position to check) -
    # mirror_ok is None only if the live exchange call itself failed, not
    # simply because a position is well-mirrored or not.
    for position in body["shadow"] + body["shadow_bingx"] + body["momentum"] + body["momentum_bingx"]:
        assert "mark_price" in position
        assert "unrealized_pnl" in position
        assert "mirror_ok" in position
    for position in body["paper"]:
        assert "mark_price" not in position


@pytest.mark.asyncio
async def test_momentum_scan_returns_a_list(client):
    resp = await client.get("/api/momentum/scan")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body["candidates"], list)
    for candidate in body["candidates"]:
        assert "symbol" in candidate
        assert "momentum_score" in candidate


@pytest.mark.asyncio
async def test_news_recent_returns_a_list(client):
    resp = await client.get("/api/news/recent", params={"limit": 5})
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body["items"], list)
    assert len(body["items"]) <= 5


@pytest.mark.asyncio
async def test_news_asset_status_returns_a_list(client):
    resp = await client.get("/api/news/asset-status")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body["statuses"], list)
    for status in body["statuses"]:
        assert "asset" in status
        assert "status" in status


@pytest.mark.asyncio
async def test_events_upcoming_returns_a_list(client):
    resp = await client.get("/api/events/upcoming", params={"limit": 10})
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body["events"], list)
    assert len(body["events"]) <= 10
    for event in body["events"]:
        assert "title" in event
        assert "coins" in event


@pytest.mark.asyncio
async def test_trades_requires_a_known_account(client):
    resp = await client.get("/api/trades", params={"account": "not_a_real_account"})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_trades_returns_a_list_for_paper(client):
    resp = await client.get("/api/trades", params={"account": "paper", "limit": 5})
    assert resp.status_code == 200
    body = resp.json()
    assert body["account"] == "paper"
    assert isinstance(body["trades"], list)
    assert len(body["trades"]) <= 5


@pytest.mark.asyncio
async def test_trades_returns_a_list_for_shadow_bingx(client):
    resp = await client.get("/api/trades", params={"account": "shadow_bingx", "limit": 5})
    assert resp.status_code == 200
    body = resp.json()
    assert body["account"] == "shadow_bingx"
    assert isinstance(body["trades"], list)
    assert len(body["trades"]) <= 5


@pytest.mark.asyncio
async def test_trades_returns_a_list_for_momentum(client):
    resp = await client.get("/api/trades", params={"account": "momentum", "limit": 5})
    assert resp.status_code == 200
    body = resp.json()
    assert body["account"] == "momentum"
    assert isinstance(body["trades"], list)
    assert len(body["trades"]) <= 5


@pytest.mark.asyncio
async def test_trades_returns_a_list_for_momentum_bingx(client):
    resp = await client.get("/api/trades", params={"account": "momentum_bingx", "limit": 5})
    assert resp.status_code == 200
    body = resp.json()
    assert body["account"] == "momentum_bingx"
    assert isinstance(body["trades"], list)


@pytest.mark.asyncio
async def test_equity_history_starts_at_the_seed_value_and_ends_at_a_real_number(client):
    resp = await client.get("/api/equity-history", params={"account": "paper", "limit": 500})
    assert resp.status_code == 200
    body = resp.json()
    assert body["account"] == "paper"
    points = body["points"]
    assert points[0]["equity"] == body["starting_equity"]
    assert points[0]["closed_at"] is None  # the seed point, before any trade
    # non-decreasing count, chronological order
    closed_ats = [p["closed_at"] for p in points[1:]]
    assert closed_ats == sorted(closed_ats)


@pytest.mark.asyncio
async def test_equity_history_requires_a_known_account(client):
    resp = await client.get("/api/equity-history", params={"account": "not_a_real_account"})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_journal_merges_all_tracked_accounts_sorted_by_closed_at(client):
    resp = await client.get("/api/journal", params={"limit": 10})
    assert resp.status_code == 200
    body = resp.json()
    entries = body["entries"]
    assert isinstance(entries, list)
    assert len(entries) <= 10
    for entry in entries:
        assert entry["account"] in ("paper", "shadow", "shadow_bingx", "momentum", "momentum_bingx")
        assert "symbol" in entry
        assert "net_pnl" in entry
    # newest first
    closed_ats = [e["closed_at"] for e in entries]
    assert closed_ats == sorted(closed_ats, reverse=True)


@pytest.mark.asyncio
async def test_backtests_returns_a_list(client):
    resp = await client.get("/api/backtests", params={"limit": 5})
    assert resp.status_code == 200
    assert isinstance(resp.json()["runs"], list)


@pytest.mark.asyncio
async def test_system_health_covers_every_configured_symbol_and_interval(client):
    settings = get_settings()
    resp = await client.get("/api/system/health")
    assert resp.status_code == 200
    rows = resp.json()["candles"]
    seen = {(r["symbol"], r["interval"]) for r in rows}
    expected = {(s, i) for s in settings.symbols for i in settings.intervals}
    assert seen == expected


@pytest.mark.asyncio
async def test_kill_switch_events_requires_a_known_account(client):
    resp = await client.get("/api/kill-switch/not_a_real_account/events")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_kill_switch_reset_requires_a_known_account(client):
    resp = await client.post("/api/kill-switch/not_a_real_account/reset", json={"note": "test"})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_kill_switch_reset_requires_a_non_empty_note(client):
    resp = await client.post("/api/kill-switch/paper/reset", json={"note": "   "})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_kill_switch_reset_conflicts_when_not_currently_triggered(client):
    # Check real state first - only assert the 409 path when we KNOW the
    # switch isn't triggered, so this test never risks clearing a real,
    # deliberate trigger on the actual "paper" account.
    overview = (await client.get("/api/overview")).json()
    if overview["accounts"]["paper"].get("kill_switch", {}).get("is_triggered", False):
        pytest.skip("paper account's kill switch is genuinely triggered right now - not safe to probe reset here")
    resp = await client.post("/api/kill-switch/paper/reset", json={"note": "api test - should not apply"})
    assert resp.status_code == 409
