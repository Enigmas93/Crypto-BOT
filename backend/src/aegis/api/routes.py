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
    get_bingx_rest,
    get_binance_rest,
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
from aegis.providers.bingx.rest_client import BingXFuturesRestClient, BingXOrderError, BingXRestError
from aegis.providers.binance.rest_client import BinanceFuturesRestClient, BinanceOrderError, BinanceRestError
from aegis.risk.rules import compute_drawdown_pct

router = APIRouter()

_TRACKED_ACCOUNTS = ("paper", "shadow", "shadow_bingx", "momentum", "momentum_bingx")
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
async def _enrich_with_live_state(
    positions: list[dict], rest_client: BinanceFuturesRestClient | BingXFuturesRestClient,
) -> None:
    """Mutates each position dict in place with what the exchange ITSELF
    reports right now (mark_price, unrealized_pnl, mirror_ok) - the
    dashboard must never show a position as "open" purely because a local
    DB row says so without also showing whether the exchange still agrees.
    `mirror_ok=False` (local says open, exchange says flat) is exactly the
    failure mode a stuck/un-reconciled local record produces - surfaced
    here instead of silently trusting local state, which is what let a
    real bug (BingXExecutionProvider.cancel_leftover_order not recognizing
    one of BingX's "already gone" error shapes) hide a stale "open"
    position on the dashboard after it was actually already closed on the
    exchange. Never fabricates a number - a position this call can't reach
    the exchange for is left with mark_price/unrealized_pnl absent
    (None), not a guessed value."""
    for position in positions:
        position["mark_price"] = None
        position["unrealized_pnl"] = None
        position["mirror_ok"] = None
        try:
            live = await rest_client.get_position_risk(position["symbol"])
        except (BinanceRestError, BinanceOrderError, BingXRestError, BingXOrderError):
            continue  # exchange unreachable/misconfigured - leave as "unknown", never guessed
        live_amt = live[0].position_amt if live else 0.0
        position["mirror_ok"] = live_amt != 0
        if live_amt != 0:
            position["mark_price"] = live[0].mark_price
            position["unrealized_pnl"] = live[0].unrealized_pnl


@router.get("/positions")
async def get_positions(
    settings: Settings = Depends(get_settings_dep),
    paper_repo: PaperRepository = Depends(get_paper_repo),
    shadow_repo: ShadowRepository = Depends(get_shadow_repo),
    momentum_repo: MomentumRepository = Depends(get_momentum_repo),
    binance_rest: BinanceFuturesRestClient = Depends(get_binance_rest),
    bingx_rest: BingXFuturesRestClient = Depends(get_bingx_rest),
) -> dict:
    positions: dict[str, list[dict]] = {
        "paper": [], "shadow": [], "shadow_bingx": [], "momentum": [], "momentum_bingx": [],
    }
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
        # BingX Shadow Trading (Fase 16) - same engine/table shape as
        # Binance's "shadow" account, just a different account_id and a
        # real order placed on a different exchange.
        shadow_bingx_position = await shadow_repo.get_open_position("shadow_bingx", symbol)
        if shadow_bingx_position is not None:
            positions["shadow_bingx"].append({"symbol": symbol, "side": shadow_bingx_position.side,
                                               "entry_price": shadow_bingx_position.entry_price,
                                               "quantity": shadow_bingx_position.quantity,
                                               "stop_price": shadow_bingx_position.stop_price,
                                               "take_profit_price": shadow_bingx_position.take_profit_price,
                                               "entry_time": shadow_bingx_position.entry_time})
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
    # BingX Momentum Engine (Fase 16) - same dynamic-universe caveat as
    # Binance's "momentum" account above.
    for symbol in await momentum_repo.get_open_symbols("momentum_bingx"):
        momentum_bingx_position = await momentum_repo.get_open_position("momentum_bingx", symbol)
        if momentum_bingx_position is not None:
            positions["momentum_bingx"].append({"symbol": symbol, "side": momentum_bingx_position.side,
                                                 "entry_price": momentum_bingx_position.entry_price,
                                                 "quantity": momentum_bingx_position.quantity,
                                                 "stop_price": momentum_bingx_position.stop_price,
                                                 "momentum_score": momentum_bingx_position.momentum_score,
                                                 "entry_time": momentum_bingx_position.entry_time})

    # Live exchange-side enrichment (mark price, unrealized PnL, and
    # whether the exchange still agrees a position is actually open) -
    # "paper" is a local simulation with no real exchange position to
    # check, so it's intentionally left out here.
    await _enrich_with_live_state(positions["shadow"], binance_rest)
    await _enrich_with_live_state(positions["momentum"], binance_rest)
    await _enrich_with_live_state(positions["shadow_bingx"], bingx_rest)
    await _enrich_with_live_state(positions["momentum_bingx"], bingx_rest)
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
async def _fetch_trades_for_account(
    account: str, limit: int,
    paper_repo: PaperRepository, shadow_repo: ShadowRepository, momentum_repo: MomentumRepository,
) -> list[dict]:
    if account == "paper":
        return await paper_repo.fetch_trades("paper", limit=limit)
    if account == "shadow":
        return await shadow_repo.fetch_trades("shadow", limit=limit)
    if account == "shadow_bingx":
        return await shadow_repo.fetch_trades("shadow_bingx", limit=limit)
    if account == "momentum":
        return await momentum_repo.fetch_trades("momentum", limit=limit)
    if account == "momentum_bingx":
        return await momentum_repo.fetch_trades("momentum_bingx", limit=limit)
    raise HTTPException(
        status_code=400,
        detail="account must be 'paper', 'shadow', 'shadow_bingx', 'momentum' or 'momentum_bingx'",
    )


@router.get("/trades")
async def get_trades(
    account: str, limit: int = 20,
    paper_repo: PaperRepository = Depends(get_paper_repo),
    shadow_repo: ShadowRepository = Depends(get_shadow_repo),
    momentum_repo: MomentumRepository = Depends(get_momentum_repo),
) -> dict:
    trades = await _fetch_trades_for_account(account, limit, paper_repo, shadow_repo, momentum_repo)
    return {"account": account, "trades": trades}


# -- equity history (derived, not a separately persisted time series) ---------
_STARTING_EQUITY = 1000.0  # matches every run_*.py script's own _STARTING_EQUITY constant


@router.get("/equity-history")
async def get_equity_history(
    account: str, limit: int = 200,
    paper_repo: PaperRepository = Depends(get_paper_repo),
    shadow_repo: ShadowRepository = Depends(get_shadow_repo),
    momentum_repo: MomentumRepository = Depends(get_momentum_repo),
) -> dict:
    """Reconstructs an equity curve from real closed trades - there is no
    separate point-in-time equity snapshot table, so this replays
    `net_pnl` in chronological order starting from the same
    `_STARTING_EQUITY` every account is initialized with
    (`risk_repo.initialize_account_state`). This is REALIZED equity only
    (an open position's unrealized PnL is not included) - a real, honest
    curve derived entirely from persisted trade records, not a fabricated
    or simulated one."""
    trades = await _fetch_trades_for_account(account, limit, paper_repo, shadow_repo, momentum_repo)
    trades_asc = sorted(trades, key=lambda t: t["closed_at"])
    equity = _STARTING_EQUITY
    points = [{"closed_at": None, "equity": equity}]
    for trade in trades_asc:
        equity += trade["net_pnl"]
        points.append({"closed_at": trade["closed_at"], "net_pnl": trade["net_pnl"], "equity": equity})
    return {"account": account, "starting_equity": _STARTING_EQUITY, "points": points}


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
    paper, shadow, shadow_bingx, momentum, momentum_bingx = await asyncio.gather(
        paper_repo.fetch_trades("paper", limit=limit),
        shadow_repo.fetch_trades("shadow", limit=limit),
        shadow_repo.fetch_trades("shadow_bingx", limit=limit),
        momentum_repo.fetch_trades("momentum", limit=limit),
        momentum_repo.fetch_trades("momentum_bingx", limit=limit),
    )
    entries = (
        [{"account": "paper", **t} for t in paper]
        + [{"account": "shadow", **t} for t in shadow]
        + [{"account": "shadow_bingx", **t} for t in shadow_bingx]
        + [{"account": "momentum", **t} for t in momentum]
        + [{"account": "momentum_bingx", **t} for t in momentum_bingx]
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
