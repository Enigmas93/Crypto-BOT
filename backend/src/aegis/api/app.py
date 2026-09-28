"""Phase 11 - Dashboard API.

Read-mostly HTTP API over the repositories already built in Phases 1-10 -
no business logic lives here, this layer only shapes repository data for a
browser to render. The write endpoints (kill-switch reset, BingX
credentials/mode, strategy toggles, capital allocation) are the reason the
API is authenticated.

Fase 19 (mobile/PWA): the dashboard can be reached from a phone through an
HTTPS tunnel, with the static PWA hosted on Vercel. Every /api request
therefore needs `Authorization: Bearer <AEGIS_API_TOKEN>`. If no token is
configured the API only answers direct loopback requests that carry no
proxy headers - a tunnel forwards from localhost too, so the loopback check
alone would not be enough. CORS only matters for the Vercel origin; the
token, not CORS, is the security boundary.
"""
from __future__ import annotations

import hmac
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from aegis.config import get_settings
from aegis.db.engine import close_pool, create_pool
from aegis.providers.binance.rest_client import BinanceFuturesRestClient

FRONTEND_DIR = Path(__file__).resolve().parents[4] / "frontend"
_PROXY_HEADERS = ("x-forwarded-for", "x-forwarded-host", "x-real-ip", "forwarded", "ngrok-trace-id",
                  "cf-connecting-ip")
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def is_request_authorized(request: Request, token: str) -> bool:
    if token:
        header = request.headers.get("authorization", "")
        supplied = header[7:] if header.lower().startswith("bearer ") else ""
        return bool(supplied) and hmac.compare_digest(supplied.encode(), token.encode())
    client_host = request.client.host if request.client else ""
    return client_host in _LOOPBACK and not any(h in request.headers for h in _PROXY_HEADERS)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    settings = get_settings()
    app.state.settings = settings
    app.state.pool = await create_pool(settings)
    # Read-only, long-lived REST client used ONLY to enrich open positions
    # with live mark price / unrealized PnL (GET /api/positions). BingX
    # clients are built per request from the database instead (Fase 17), since
    # its key and demo/live mode can change at any time.
    app.state.binance_rest = BinanceFuturesRestClient(
        testnet=settings.binance_testnet, api_key=settings.binance_api_key, api_secret=settings.binance_api_secret,
    )
    try:
        yield
    finally:
        await app.state.binance_rest.aclose()
        await close_pool(app.state.pool)


def create_app(settings=None) -> FastAPI:
    boot_settings = settings or get_settings()
    app = FastAPI(title="Aegis Quant Dashboard API", lifespan=_lifespan)

    from aegis.api.routes import router  # local import - avoids a circular import at module load time

    app.include_router(router, prefix="/api")

    @app.middleware("http")
    async def _require_token(request: Request, call_next):
        if request.url.path.startswith("/api/") and request.method != "OPTIONS":
            settings = getattr(request.app.state, "settings", None) or boot_settings
            if not is_request_authorized(request, settings.aegis_api_token):
                return JSONResponse({"detail": "unauthorized"}, status_code=401)
        return await call_next(request)

    # Added after the auth middleware so it wraps it: preflights and 401s
    # both get CORS headers, letting the PWA show "token inválido" instead
    # of an opaque network error.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=boot_settings.cors_origin_list,
        allow_origin_regex=boot_settings.aegis_cors_origin_regex or None,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "ngrok-skip-browser-warning"],
    )

    app.mount("/static", StaticFiles(directory=FRONTEND_DIR / "static"), name="static")

    @app.get("/")
    async def _index() -> FileResponse:
        return FileResponse(FRONTEND_DIR / "index.html")

    @app.get("/manifest.webmanifest")
    async def _manifest() -> FileResponse:
        return FileResponse(FRONTEND_DIR / "manifest.webmanifest", media_type="application/manifest+json")

    @app.get("/sw.js")
    async def _service_worker() -> FileResponse:
        return FileResponse(FRONTEND_DIR / "sw.js", media_type="application/javascript",
                            headers={"Cache-Control": "no-cache"})

    return app


app = create_app()
