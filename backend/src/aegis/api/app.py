"""Phase 11 - Dashboard API.

Local-only, read-mostly HTTP API over the repositories already built in
Phases 1-10 - no new business logic lives here, this layer only shapes
existing repository data for a browser to render. The one exception is
the kill switch reset endpoint, which calls `KillSwitchRepository.reset`
directly (already requires an explicit note - same safety property as the
CLI path, not weakened by having an HTTP front door).

Deliberately no authentication: this binds to localhost by default and is
meant for a single operator on their own machine, the same trust boundary
every other script in this project already assumes (direct DB access, a
`.env` with real API keys, etc.). Documented explicitly in the README's
Riscos section - never expose this port to the public internet without
adding auth first.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from aegis.config import get_settings
from aegis.db.engine import close_pool, create_pool
from aegis.providers.binance.rest_client import BinanceFuturesRestClient

_STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def _lifespan(app: FastAPI):
    settings = get_settings()
    app.state.settings = settings
    app.state.pool = await create_pool(settings)
    # Read-only, long-lived REST client used ONLY to enrich open positions
    # with live mark price / unrealized PnL (GET /api/positions) - never to
    # place or cancel anything from this dashboard. Built even without
    # credentials configured; every signed call already fails safely
    # (BinanceOrderError) and callers treat that as "no live enrichment
    # available" rather than crashing the endpoint.
    #
    # BingX has no equivalent long-lived client here (Fase 17): its key and
    # demo/live mode are managed from the dashboard and can change at any
    # time, so /api/positions builds short-lived BingX clients per request
    # from whatever's currently in bingx_account_settings instead of one
    # fixed at startup - see aegis.api.routes.get_positions.
    app.state.binance_rest = BinanceFuturesRestClient(
        testnet=settings.binance_testnet, api_key=settings.binance_api_key, api_secret=settings.binance_api_secret,
    )
    try:
        yield
    finally:
        await app.state.binance_rest.aclose()
        await close_pool(app.state.pool)


def create_app() -> FastAPI:
    app = FastAPI(title="Aegis Quant Dashboard API", lifespan=_lifespan)

    from aegis.api.routes import router  # local import - avoids a circular import at module load time

    app.include_router(router, prefix="/api")
    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

    @app.get("/")
    async def _index() -> FileResponse:
        return FileResponse(_STATIC_DIR / "index.html")

    return app


app = create_app()
