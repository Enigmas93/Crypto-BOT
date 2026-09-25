"""FastAPI dependency wiring - one request-scoped repository instance per
repository, all sharing the single pool created once at app startup
(app.state.pool, see app.py's lifespan). Repositories are cheap wrappers
around the pool, so building a fresh one per request costs nothing real.
"""
from __future__ import annotations

from fastapi import Request

from aegis.config import Settings
from aegis.db.backtest_repository import BacktestRepository
from aegis.db.bingx_account_repository import BingxAccountRepository
from aegis.db.candle_repository import CandleRepository
from aegis.db.derivatives_repository import DerivativesRepository
from aegis.db.kill_switch_repository import KillSwitchRepository
from aegis.db.events_repository import EventsRepository
from aegis.db.liquidation_repository import LiquidationRepository
from aegis.db.macro_repository import MacroRepository
from aegis.db.momentum_repository import MomentumRepository
from aegis.db.news_repository import NewsRepository
from aegis.db.paper_repository import PaperRepository
from aegis.db.risk_repository import RiskRepository
from aegis.db.shadow_repository import ShadowRepository
from aegis.db.capital_allocation_repository import CapitalAllocationRepository
from aegis.db.strategy_settings_repository import StrategySettingsRepository
from aegis.db.walk_forward_repository import WalkForwardRepository
from aegis.providers.binance.rest_client import BinanceFuturesRestClient


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


def get_binance_rest(request: Request) -> BinanceFuturesRestClient:
    return request.app.state.binance_rest


def get_bingx_account_repo(request: Request) -> BingxAccountRepository:
    """Fase 17: the BingX key/secret + demo/live mode live in the database,
    managed from the dashboard - not a long-lived app.state client built
    once from .env, since that would never notice a mode switch."""
    return BingxAccountRepository(request.app.state.pool, request.app.state.settings.credential_encryption_key)


def get_derivatives_repo(request: Request) -> DerivativesRepository:
    return DerivativesRepository(request.app.state.pool)


def get_liquidation_repo(request: Request) -> LiquidationRepository:
    return LiquidationRepository(request.app.state.pool)


def get_macro_repo(request: Request) -> MacroRepository:
    return MacroRepository(request.app.state.pool)


def get_walk_forward_repo(request: Request) -> WalkForwardRepository:
    return WalkForwardRepository(request.app.state.pool)


def get_strategy_settings_repo(request: Request) -> StrategySettingsRepository:
    return StrategySettingsRepository(request.app.state.pool)


def get_capital_allocation_repo(request: Request) -> CapitalAllocationRepository:
    return CapitalAllocationRepository(request.app.state.pool)
