"""Dashboard API routes (Phase 11). One router, grouped by concern in
comments rather than split into many files - the endpoint count is small
enough that splitting further would cost more (import wiring) than it
saves (navigation).

Every endpoint is read-only except POST /kill-switch/{account_id}/reset -
see app.py's module docstring for why that one write endpoint is safe to
expose the same way the CLI path already is.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from aegis.api.dependencies import (
    get_backtest_repo,
    get_candle_repo,
    get_events_repo,
    get_kill_switch_repo,
    get_momentum_repo,
    get_news_repo,
    get_paper_repo,
    get_risk_repo,
    get_settings_dep,
    get_shadow_repo,
)
from aegis.config import Settings
from aegis.db.backtest_repository import BacktestRepository
from aegis.db.candle_repository import CandleRepository
from aegis.db.events_repository import EventsRepository
from aegis.db.kill_switch_repository import KillSwitchRepository
from aegis.db.momentum_repository import MomentumRepository
from aegis.db.news_repository import NewsRepository
from aegis.db.paper_repository import PaperRepository
from aegis.db.risk_repository import RiskRepository
from aegis.db.shadow_repository import ShadowRepository
from aegis.risk.rules import compute_drawdown_pct

router = APIRouter()

_TRACKED_ACCOUNTS = ("paper", "shadow", "momentum")
_TRADING_INTERVAL = "1h"  # the interval Paper/Shadow Trading actually act on (Phase 9/10)


# -- overview -----------------------------------------------------------
@router.get("/overview")
async def get_overview(risk_repo: RiskRepository = Depends(get_risk_repo),
                        kill_switch_repo: KillSwitchRepository = Depends(get_kill_switch_repo)) -> dict:
    accounts = {}
    for account_id in _TRACKED_ACCOUNTS:
        account = await risk_repo.get_account_state(account_id)
        kill_state = await kill_switch_repo.get_state(account_id)
        if account is None:
            accounts[account_id] = {"initialized": False}
            continue
        accounts[account_id] = {
            "initialized": True,
            "equity": account.equity,
            "peak_equity": account.peak_equity,
            "drawdown_pct": compute_drawdown_pct(account.equity, account.peak_equity),
            "daily_realized_pnl": account.daily_realized_pnl,
            "consecutive_losses": account.consecutive_losses,
            "open_positions_count": account.open_positions_count,
            "kill_switch": {"is_triggered": kill_state.is_triggered, "reasons": kill_state.reasons},
        }
    return {"accounts": accounts}


# -- positions ------------------------------------------------------------
@router.get("/positions")
async def get_positions(
    settings: Settings = Depends(get_settings_dep),
    paper_repo: PaperRepository = Depends(get_paper_repo),
    shadow_repo: ShadowRepository = Depends(get_shadow_repo),
    momentum_repo: MomentumRepository = Depends(get_momentum_repo),
) -> dict:
    positions: dict[str, list[dict]] = {"paper": [], "shadow": [], "momentum": []}
    for symbol in settings.symbols:
        paper_position = await paper_repo.get_open_position("paper", symbol)
        if paper_position is not None:
            positions["paper"].append({"symbol": symbol, "side": paper_position.side,
                                        "entry_price": paper_position.entry_price,
                                        "quantity": paper_position.quantity,
                                        "stop_price": paper_position.stop_price,
                                        "take_profit_price": paper_position.take_profit_price,
                                        "entry_time": paper_position.entry_time})
        shadow_position = await shadow_repo.get_open_position("shadow", symbol)
        if shadow_position is not None:
            positions["shadow"].append({"symbol": symbol, "side": shadow_position.side,
                                         "entry_price": shadow_position.entry_price,
                                         "quantity": shadow_position.quantity,
                                         "stop_price": shadow_position.stop_price,
                                         "take_profit_price": shadow_position.take_profit_price,
                                         "entry_time": shadow_position.entry_time})
    # Momentum's symbol universe is the Scanner's dynamic top-N, not the
    # fixed settings.symbols list - an open position can be in a symbol
    # never seen by any other engine (e.g. MUBARAKUSDT), so it must be
    # discovered via get_open_symbols rather than looped like the two lists
    # above. No take_profit_price - this engine's exit is a trailing stop,
    # not a fixed target (see aegis/momentum/engine.py).
    for symbol in await momentum_repo.get_open_symbols("momentum"):
        momentum_position = await momentum_repo.get_open_position("momentum", symbol)
        if momentum_position is not None:
            positions["momentum"].append({"symbol": symbol, "side": momentum_position.side,
                                           "entry_price": momentum_position.entry_price,
                                           "quantity": momentum_position.quantity,
                                           "stop_price": momentum_position.stop_price,
                                           "momentum_score": momentum_position.momentum_score,
                                           "entry_time": momentum_position.entry_time})
    return positions


# -- momentum scanner -------------------------------------------------------
@router.get("/momentum/scan")
async def get_momentum_scan(momentum_repo: MomentumRepository = Depends(get_momentum_repo)) -> dict:
    return {"candidates": await momentum_repo.fetch_latest_scan_results()}


# -- news ---------------------------------------------------------------
@router.get("/news/recent")
async def get_news_recent(limit: int = 20, news_repo: NewsRepository = Depends(get_news_repo)) -> dict:
    return {"items": await news_repo.fetch_recent(limit=limit)}


@router.get("/news/asset-status")
async def get_news_asset_status(news_repo: NewsRepository = Depends(get_news_repo)) -> dict:
    return {"statuses": await news_repo.fetch_all_asset_statuses()}


# -- event risk (CoinMarketCal) -----------------------------------------------
@router.get("/events/upcoming")
async def get_upcoming_events(limit: int = 20, events_repo: EventsRepository = Depends(get_events_repo)) -> dict:
    return {"events": await events_repo.fetch_upcoming(since=datetime.now(UTC), limit=limit)}


# -- trades ---------------------------------------------------------------
@router.get("/trades")
async def get_trades(
    account: str, limit: int = 20,
    paper_repo: PaperRepository = Depends(get_paper_repo),
    shadow_repo: ShadowRepository = Depends(get_shadow_repo),
    momentum_repo: MomentumRepository = Depends(get_momentum_repo),
) -> dict:
    if account == "paper":
        trades = await paper_repo.fetch_trades("paper", limit=limit)
    elif account == "shadow":
        trades = await shadow_repo.fetch_trades("shadow", limit=limit)
    elif account == "momentum":
        trades = await momentum_repo.fetch_trades("momentum", limit=limit)
    else:
        raise HTTPException(status_code=400, detail="account must be 'paper', 'shadow' or 'momentum'")
    return {"account": account, "trades": trades}


# -- trading journal ----------------------------------------------------------
@router.get("/journal")
async def get_journal(
    limit: int = 30,
    paper_repo: PaperRepository = Depends(get_paper_repo),
    shadow_repo: ShadowRepository = Depends(get_shadow_repo),
    momentum_repo: MomentumRepository = Depends(get_momentum_repo),
) -> dict:
    """Every real account's closed trades, merged into one chronological
    timeline - the three per-account trade tables already on the dashboard
    show what happened where; this answers "what has the bot actually done,
    in order" across all of them at once. Each repo is asked for `limit`
    trades on its own (cheap - these tables are still small) so merging
    never has to worry about one account's older trades getting starved out
    by another's more active one before the final sort/truncate."""
    paper, shadow, momentum = await asyncio.gather(
        paper_repo.fetch_trades("paper", limit=limit),
        shadow_repo.fetch_trades("shadow", limit=limit),
        momentum_repo.fetch_trades("momentum", limit=limit),
    )
    entries = (
        [{"account": "paper", **t} for t in paper]
        + [{"account": "shadow", **t} for t in shadow]
        + [{"account": "momentum", **t} for t in momentum]
    )
    entries.sort(key=lambda t: t["closed_at"], reverse=True)
    return {"entries": entries[:limit]}


# -- backtests --------------------------------------------------------------
@router.get("/backtests")
async def get_backtests(limit: int = 20, backtest_repo: BacktestRepository = Depends(get_backtest_repo)) -> dict:
    return {"runs": await backtest_repo.fetch_recent_runs(limit=limit)}


# -- kill switch ------------------------------------------------------------
@router.get("/kill-switch/{account_id}/events")
async def get_kill_switch_events(
    account_id: str, limit: int = 20, kill_switch_repo: KillSwitchRepository = Depends(get_kill_switch_repo),
) -> dict:
    if account_id not in _TRACKED_ACCOUNTS:
        raise HTTPException(status_code=404, detail=f"unknown account_id {account_id!r}")
    return {"account_id": account_id, "events": await kill_switch_repo.fetch_events(account_id, limit=limit)}


class KillSwitchResetRequest(BaseModel):
    note: str


@router.post("/kill-switch/{account_id}/reset")
async def reset_kill_switch(
    account_id: str, body: KillSwitchResetRequest,
    kill_switch_repo: KillSwitchRepository = Depends(get_kill_switch_repo),
) -> dict:
    if account_id not in _TRACKED_ACCOUNTS:
        raise HTTPException(status_code=404, detail=f"unknown account_id {account_id!r}")
    if not body.note.strip():
        raise HTTPException(status_code=400, detail="note is required - resets must be auditable")
    try:
        state = await kill_switch_repo.reset(account_id, note=body.note)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"account_id": account_id, "is_triggered": state.is_triggered}


# -- system health ------------------------------------------------------------
@router.get("/system/health")
async def get_system_health(
    settings: Settings = Depends(get_settings_dep), candle_repo: CandleRepository = Depends(get_candle_repo),
) -> dict:
    now = datetime.now(UTC)
    rows = []
    for symbol in settings.symbols:
        for interval in settings.intervals:
            df = await candle_repo.fetch_ohlcv(symbol, interval, limit=1, closed_only=True)
            if df.empty:
                rows.append({"symbol": symbol, "interval": interval, "latest_close_time": None,
                             "stale": True, "age_seconds": None})
                continue
            latest_close = df["close_time"].iloc[0].to_pydatetime()
            age_seconds = (now - latest_close).total_seconds()
            rows.append({
                "symbol": symbol, "interval": interval, "latest_close_time": latest_close,
                "age_seconds": age_seconds, "stale": age_seconds > _stale_threshold_seconds(interval),
            })
    return {"candles": rows}


def _stale_threshold_seconds(interval: str) -> float:
    """A candle is 'stale' if it's more than 2x its own interval old - a
    generous margin (a 1h candle isn't stale 5 minutes after the hour
    turns over) that still catches a genuinely dead collector."""
    unit_seconds = {"m": 60, "h": 3600, "d": 86400}
    try:
        value = int(interval[:-1])
        unit = unit_seconds[interval[-1]]
    except (ValueError, KeyError):
        return 3600.0  # unknown interval format - a conservative default, not a crash
    return value * unit * 2
