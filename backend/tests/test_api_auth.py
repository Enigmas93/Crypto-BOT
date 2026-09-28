"""Dashboard API access control (Fase 19): the API is reachable from a phone
through a tunnel, so every /api call needs the bearer token - no DB needed."""
import httpx
import pytest

from aegis.api.app import create_app
from aegis.config import Settings


def _client(token: str, **headers) -> httpx.AsyncClient:
    settings = Settings(_env_file=None, aegis_api_token=token)
    app = create_app(settings)
    app.state.settings = settings
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers)


@pytest.mark.asyncio
async def test_token_required_when_configured():
    async with _client("s3cret") as c:
        assert (await c.get("/api/auth/check")).status_code == 401
        assert (await c.get("/api/auth/check", headers={"Authorization": "Bearer wrong"})).status_code == 401
        ok = await c.get("/api/auth/check", headers={"Authorization": "Bearer s3cret"})
        assert ok.status_code == 200 and ok.json() == {"ok": True}


@pytest.mark.asyncio
async def test_without_token_only_direct_loopback_is_allowed():
    async with _client("") as c:
        assert (await c.get("/api/auth/check")).status_code == 200
        # A tunnel also connects from localhost - its proxy headers must not pass.
        tunneled = await c.get("/api/auth/check", headers={"X-Forwarded-For": "203.0.113.9"})
        assert tunneled.status_code == 401
        assert (await c.get("/api/auth/check", headers={"ngrok-trace-id": "abc"})).status_code == 401


@pytest.mark.asyncio
async def test_cors_preflight_from_the_vercel_app_is_answered():
    async with _client("s3cret") as c:
        resp = await c.options("/api/overview", headers={
            "Origin": "https://aegis-quant-chi.vercel.app", "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization,ngrok-skip-browser-warning",
        })
        assert resp.status_code == 200
        assert resp.headers["access-control-allow-origin"] == "https://aegis-quant-chi.vercel.app"


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["https://evil.example.com", "https://aegis-quant.vercel.app",
                                    "https://aegis-quant-abc-otherteam.vercel.app"])
async def test_foreign_origins_get_no_cors_grant(origin):
    async with _client("s3cret") as c:
        resp = await c.options("/api/overview", headers={"Origin": origin, "Access-Control-Request-Method": "GET"})
        assert "access-control-allow-origin" not in resp.headers


@pytest.mark.asyncio
async def test_pwa_shell_is_public():
    async with _client("s3cret") as c:
        assert "text/html" in (await c.get("/")).headers["content-type"]
        assert (await c.get("/manifest.webmanifest")).status_code == 200
        assert (await c.get("/sw.js")).status_code == 200
        assert (await c.get("/static/dashboard.js")).status_code == 200
