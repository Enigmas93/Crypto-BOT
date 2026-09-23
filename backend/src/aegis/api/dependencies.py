"""FastAPI dependency wiring - one request-scoped repository instance per
repository, all sharing the single pool created once at app startup
(app.state.pool, see app.py's lifespan). Repositories are cheap wrappers
around the pool, so building a fresh one per request costs nothing real.
"""
from __future__ import annotations

from fastapi import Request

from aegis.config import Settings
from aegis.db.backtest_repository import BacktestRepository
from aegis.db.candle_repository import CandleRepository
from aegis.db.kill_switch_repository import KillSwitchRepository
from aegis.db.events_repository import EventsRepository
from aegis.db.momentum_repository import MomentumRepository
from aegis.db.news_repository import NewsRepository
from aegis.db.paper_repository import PaperRepository
from aegis.db.risk_repository import RiskRepository
from aegis.db.shadow_repository import ShadowRepository


def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings


def get_risk_repo(request: Request) -> RiskRepository:
    return RiskRepository(request.app.state.pool)


def get_kill_switch_repo(request: Request) -> KillSwitchRepository:
    return KillSwitchRepository(request.app.state.pool)


def get_paper_repo(request: Request) -> PaperRepository:
    return PaperRepository(request.app.state.pool)


def get_shadow_repo(request: Request) -> ShadowRepository:
    return ShadowRepository(request.app.state.pool)


def get_momentum_repo(request: Request) -> MomentumRepository:
    return MomentumRepository(request.app.state.pool)


def get_news_repo(request: Request) -> NewsRepository:
    return NewsRepository(request.app.state.pool)


def get_events_repo(request: Request) -> EventsRepository:
    return EventsRepository(request.app.state.pool)


def get_backtest_repo(request: Request) -> BacktestRepository:
    return BacktestRepository(request.app.state.pool)


def get_candle_repo(request: Request) -> CandleRepository:
    return CandleRepository(request.app.state.pool)
