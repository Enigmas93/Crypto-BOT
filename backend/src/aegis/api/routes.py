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
    get_bingx_account_repo,
    get_binance_rest,
    get_candle_repo,
    get_derivatives_repo,
    get_events_repo,
    get_kill_switch_repo,
    get_liquidation_repo,
    get_macro_repo,
    get_momentum_repo,
    get_news_repo,
    get_paper_repo,
    get_risk_repo,
    get_capital_allocation_repo,
    get_settings_dep,
    get_shadow_repo,
    get_strategy_settings_repo,
    get_walk_forward_repo,
)
from aegis.config import Settings
from aegis.db.backtest_repository import BacktestRepository
from aegis.db.bingx_account_repository import BingxAccountRepository
from aegis.db.candle_repository import CandleRepository
from aegis.db.derivatives_repository import DerivativesRepository
from aegis.db.events_repository import EventsRepository
from aegis.db.kill_switch_repository import KillSwitchRepository
from aegis.db.liquidation_repository import LiquidationRepository
from aegis.db.macro_repository import MacroRepository
from aegis.db.momentum_repository import MomentumRepository
from aegis.db.news_repository import NewsRepository
from aegis.db.paper_repository import PaperRepository
from aegis.db.risk_repository import RiskRepository
from aegis.db.capital_allocation_repository import CapitalAllocationRepository
from aegis.db.shadow_repository import ShadowRepository
from aegis.db.strategy_settings_repository import StrategySettingsRepository
from aegis.db.walk_forward_repository import WalkForwardRepository
from aegis.providers.bingx.rest_client import BingXFuturesRestClient, BingXOrderError, BingXRestError
from aegis.providers.binance.rest_client import BinanceFuturesRestClient, BinanceOrderError, BinanceRestError
from aegis.regime.service import classify_regime
from aegis.risk.rules import compute_drawdown_pct
from aegis.strategy.strategies import ALL_STRATEGY_IDS
from aegis.technical.service import compute_snapshot

router = APIRouter()

# Fase 17: shadow_bingx/momentum_bingx split into _demo/_live variants - see
# migration 0018 and aegis.execution.bingx_session for why (same BingX key,
# but demo and real balances are different exchange-side accounts, so their
# trade history/risk state must never mix).
_TRACKED_ACCOUNTS = (
    "paper", "shadow", "shadow_bingx_demo", "shadow_bingx_live",
    "momentum", "momentum_bingx_demo", "momentum_bingx_live",
)
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


async def _bingx_rest_clients(bingx_account_repo: BingxAccountRepository) -> tuple[
    BingXFuturesRestClient, BingXFuturesRestClient,
]:
    """Fase 17: BingX uses ONE key for both demo (VST) and live balances, so
    both can always be read back regardless of which mode is currently
    "active" for trading - built fresh per request (not app.state) so a key
    saved/rotated from Configurações is reflected immediately. Callers must
    aclose() both when done."""
    credentials = await bingx_account_repo.get_decrypted_credentials()
    api_key, api_secret = credentials if credentials is not None else ("", "")
    demo = BingXFuturesRestClient(testnet=True, api_key=api_key, api_secret=api_secret)
    live = BingXFuturesRestClient(testnet=False, api_key=api_key, api_secret=api_secret)
    return demo, live


@router.get("/positions")
async def get_positions(
    settings: Settings = Depends(get_settings_dep),
    paper_repo: PaperRepository = Depends(get_paper_repo),
    shadow_repo: ShadowRepository = Depends(get_shadow_repo),
    momentum_repo: MomentumRepository = Depends(get_momentum_repo),
    binance_rest: BinanceFuturesRestClient = Depends(get_binance_rest),
    bingx_account_repo: BingxAccountRepository = Depends(get_bingx_account_repo),
) -> dict:
    positions: dict[str, list[dict]] = {
        "paper": [], "shadow": [], "shadow_bingx_demo": [], "shadow_bingx_live": [],
        "momentum": [], "momentum_bingx_demo": [], "momentum_bingx_live": [],
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
        # BingX Shadow Trading (Fase 16/17) - same engine/table shape as
        # Binance's "shadow" account, just a different account_id per mode
        # and a real order placed on a different exchange.
        for account_id in ("shadow_bingx_demo", "shadow_bingx_live"):
            bingx_position = await shadow_repo.get_open_position(account_id, symbol)
            if bingx_position is not None:
                positions[account_id].append({"symbol": symbol, "side": bingx_position.side,
                                               "entry_price": bingx_position.entry_price,
                                               "quantity": bingx_position.quantity,
                                               "stop_price": bingx_position.stop_price,
                                               "take_profit_price": bingx_position.take_profit_price,
                                               "entry_time": bingx_position.entry_time})
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
    # BingX Momentum Engine (Fase 16/17) - same dynamic-universe caveat as
    # Binance's "momentum" account above, once per mode.
    for account_id in ("momentum_bingx_demo", "momentum_bingx_live"):
        for symbol in await momentum_repo.get_open_symbols(account_id):
            bingx_momentum_position = await momentum_repo.get_open_position(account_id, symbol)
            if bingx_momentum_position is not None:
                positions[account_id].append({"symbol": symbol, "side": bingx_momentum_position.side,
                                               "entry_price": bingx_momentum_position.entry_price,
                                               "quantity": bingx_momentum_position.quantity,
                                               "stop_price": bingx_momentum_position.stop_price,
                                               "momentum_score": bingx_momentum_position.momentum_score,
                                               "entry_time": bingx_momentum_position.entry_time})

    # Live exchange-side enrichment (mark price, unrealized PnL, and
    # whether the exchange still agrees a position is actually open) -
    # "paper" is a local simulation with no real exchange position to
    # check, so it's intentionally left out here.
    bingx_demo_rest, bingx_live_rest = await _bingx_rest_clients(bingx_account_repo)
    try:
        await _enrich_with_live_state(positions["shadow"], binance_rest)
        await _enrich_with_live_state(positions["momentum"], binance_rest)
        await _enrich_with_live_state(positions["shadow_bingx_demo"], bingx_demo_rest)
        await _enrich_with_live_state(positions["shadow_bingx_live"], bingx_live_rest)
        await _enrich_with_live_state(positions["momentum_bingx_demo"], bingx_demo_rest)
        await _enrich_with_live_state(positions["momentum_bingx_live"], bingx_live_rest)
    finally:
        await bingx_demo_rest.aclose()
        await bingx_live_rest.aclose()
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
    if account in ("shadow_bingx_demo", "shadow_bingx_live"):
        return await shadow_repo.fetch_trades(account, limit=limit)
    if account == "momentum":
        return await momentum_repo.fetch_trades("momentum", limit=limit)
    if account in ("momentum_bingx_demo", "momentum_bingx_live"):
        return await momentum_repo.fetch_trades(account, limit=limit)
    raise HTTPException(
        status_code=400,
        detail="account must be 'paper', 'shadow', 'shadow_bingx_demo', 'shadow_bingx_live', "
                "'momentum', 'momentum_bingx_demo' or 'momentum_bingx_live'",
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
    paper, shadow, shadow_bingx_demo, shadow_bingx_live, momentum, momentum_bingx_demo, momentum_bingx_live = (
        await asyncio.gather(
            paper_repo.fetch_trades("paper", limit=limit),
            shadow_repo.fetch_trades("shadow", limit=limit),
            shadow_repo.fetch_trades("shadow_bingx_demo", limit=limit),
            shadow_repo.fetch_trades("shadow_bingx_live", limit=limit),
            momentum_repo.fetch_trades("momentum", limit=limit),
            momentum_repo.fetch_trades("momentum_bingx_demo", limit=limit),
            momentum_repo.fetch_trades("momentum_bingx_live", limit=limit),
        )
    )
    entries = (
        [{"account": "paper", **t} for t in paper]
        + [{"account": "shadow", **t} for t in shadow]
        + [{"account": "shadow_bingx_demo", **t} for t in shadow_bingx_demo]
        + [{"account": "shadow_bingx_live", **t} for t in shadow_bingx_live]
        + [{"account": "momentum", **t} for t in momentum]
        + [{"account": "momentum_bingx_demo", **t} for t in momentum_bingx_demo]
        + [{"account": "momentum_bingx_live", **t} for t in momentum_bingx_live]
    )
    entries.sort(key=lambda t: t["closed_at"], reverse=True)
    return {"entries": entries[:limit]}


# -- backtests --------------------------------------------------------------
@router.get("/backtests")
async def get_backtests(limit: int = 20, backtest_repo: BacktestRepository = Depends(get_backtest_repo)) -> dict:
    return {"runs": await backtest_repo.fetch_recent_runs(limit=limit)}


@router.get("/backtests/{run_id}/trades")
async def get_backtest_trades(run_id: int, backtest_repo: BacktestRepository = Depends(get_backtest_repo)) -> dict:
    """Trade-level drill-down for one backtest run - `fetch_recent_runs`
    only returns aggregate stats (win rate, net PnL); this is every
    individual simulated trade behind that number."""
    run = await backtest_repo.fetch_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"backtest run {run_id} not found")
    return {"run": run, "trades": await backtest_repo.fetch_trades(run_id)}


# -- walk-forward validation (Fase 17) ---------------------------------------
@router.get("/walk-forward")
async def get_walk_forward_runs(
    limit: int = 20, walk_forward_repo: WalkForwardRepository = Depends(get_walk_forward_repo),
) -> dict:
    return {"runs": await walk_forward_repo.fetch_recent_runs(limit=limit)}


@router.get("/walk-forward/{run_id}")
async def get_walk_forward_run(
    run_id: int, walk_forward_repo: WalkForwardRepository = Depends(get_walk_forward_repo),
) -> dict:
    run = await walk_forward_repo.fetch_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"walk-forward run {run_id} not found")
    return run


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


# -- market intelligence (Macro / Derivatives / Liquidation Engines) ------------
# These three engines have been running and persisting real data since
# Fase 4/4b/5 but had NO dashboard exposure at all until now - found during
# a full audit of every repository against what the API actually surfaced.
def _df_last_row(df) -> dict | None:
    """Converts a pandas DataFrame's last row into a plain, JSON-safe dict
    (numpy scalars -> Python scalars, Timestamps -> ISO strings) - the
    repositories below return DataFrames (built for the analytics/z-score
    math), not the plain dicts every other endpoint here already returns."""
    if df.empty:
        return None
    row = df.iloc[-1]
    out: dict = {}
    for key, value in row.items():
        if hasattr(value, "isoformat"):
            out[key] = value.isoformat()
        elif hasattr(value, "item"):
            out[key] = value.item()
        else:
            out[key] = value
    return out


@router.get("/market/macro")
async def get_macro_snapshot(macro_repo: MacroRepository = Depends(get_macro_repo)) -> dict:
    return {"series": await macro_repo.fetch_all_snapshots()}


@router.get("/market/derivatives")
async def get_derivatives_summary(
    settings: Settings = Depends(get_settings_dep),
    derivatives_repo: DerivativesRepository = Depends(get_derivatives_repo),
) -> dict:
    period = settings.derivatives_periods[0] if settings.derivatives_periods else "5m"
    rows = []
    for symbol in settings.symbols:
        funding = _df_last_row(await derivatives_repo.fetch_funding_series(symbol, limit=1))
        open_interest = _df_last_row(await derivatives_repo.fetch_open_interest_series(symbol, period, limit=1))
        long_short = _df_last_row(await derivatives_repo.fetch_long_short_series(symbol, "GLOBAL_ACCOUNT", period, limit=1))
        rows.append({
            "symbol": symbol,
            "funding_rate": funding.get("funding_rate") if funding else None,
            "mark_price": funding.get("mark_price") if funding else None,
            "open_interest": open_interest.get("sum_open_interest") if open_interest else None,
            "long_short_ratio": long_short.get("long_short_ratio") if long_short else None,
        })
    return {"period": period, "symbols": rows}


@router.get("/market/liquidations")
async def get_liquidations_summary(
    settings: Settings = Depends(get_settings_dep),
    liquidation_repo: LiquidationRepository = Depends(get_liquidation_repo),
) -> dict:
    """Trailing-1h aggregate (12 x 5-minute buckets) per symbol - a
    dashboard summary, not the full windowed series `fetch_windowed_stats`
    exists to feed the Liquidation Engine's own z-score/acceleration math."""
    rows = []
    for symbol in settings.symbols:
        df = await liquidation_repo.fetch_windowed_stats(symbol, window_seconds=300, num_buckets=12)
        rows.append({
            "symbol": symbol,
            "long_notional": float(df["long_notional"].sum()) if not df.empty else 0.0,
            "short_notional": float(df["short_notional"].sum()) if not df.empty else 0.0,
            "long_count": int(df["long_count"].sum()) if not df.empty else 0,
            "short_count": int(df["short_count"].sum()) if not df.empty else 0,
        })
    return {"window_minutes": 60, "symbols": rows}


# -- BingX account settings (Fase 17 - demo/live switch from the dashboard) ---
# BingX uses ONE key for both demo (VST) and live balances - there is no
# second credential slot, only a mode to pick. Switching to 'live' requires
# this exact confirmation phrase in the request body, on top of already
# having a saved key - a deliberate two-step gate against ever flipping to
# real money by accident (spec's "trava explícita contra o acidente
# clássico: paper -> live sem querer", applied here to demo -> live).
_LIVE_MODE_CONFIRMATION_PHRASE = "ATIVAR CONTA REAL"


class BingxCredentialsRequest(BaseModel):
    api_key: str
    api_secret: str


class BingxModeRequest(BaseModel):
    mode: str
    confirm: str | None = None


@router.get("/settings/bingx")
async def get_bingx_settings(bingx_account_repo: BingxAccountRepository = Depends(get_bingx_account_repo)) -> dict:
    state = await bingx_account_repo.get_settings()
    return {
        "mode": state.mode,
        "credentials_configured": state.credentials_configured,
        "updated_at": state.updated_at,
    }


@router.post("/settings/bingx/credentials")
async def save_bingx_credentials(
    body: BingxCredentialsRequest,
    bingx_account_repo: BingxAccountRepository = Depends(get_bingx_account_repo),
) -> dict:
    api_key = body.api_key.strip()
    api_secret = body.api_secret.strip()
    if not api_key or not api_secret:
        raise HTTPException(status_code=400, detail="api_key e api_secret são obrigatórios")
    # Validate against BingX for real before persisting anything, read-only
    # (account balance) - tried against the demo/VST endpoint since that's
    # always safe to hit and the same key works against both per BingX's
    # account model, so a working demo probe means the key itself is good.
    probe = BingXFuturesRestClient(testnet=True, api_key=api_key, api_secret=api_secret)
    try:
        await probe.get_account_balance()
    except (BingXRestError, BingXOrderError) as exc:
        raise HTTPException(status_code=400, detail=f"não foi possível validar a chave na BingX: {exc}") from exc
    finally:
        await probe.aclose()
    await bingx_account_repo.save_credentials(api_key, api_secret)
    return {"saved": True}


@router.post("/settings/bingx/mode")
async def set_bingx_mode(
    body: BingxModeRequest,
    bingx_account_repo: BingxAccountRepository = Depends(get_bingx_account_repo),
    shadow_repo: ShadowRepository = Depends(get_shadow_repo),
    momentum_repo: MomentumRepository = Depends(get_momentum_repo),
) -> dict:
    if body.mode not in ("demo", "live"):
        raise HTTPException(status_code=400, detail="mode deve ser 'demo' ou 'live'")
    current = await bingx_account_repo.get_settings()
    if body.mode == current.mode:
        return {"mode": current.mode}
    if body.mode == "live":
        if not current.credentials_configured:
            raise HTTPException(status_code=400, detail="configure uma chave da BingX antes de ativar a conta real")
        if body.confirm != _LIVE_MODE_CONFIRMATION_PHRASE:
            raise HTTPException(
                status_code=400,
                detail=f"para ativar a conta REAL, envie confirm={_LIVE_MODE_CONFIRMATION_PHRASE!r} exatamente",
            )
    # Never switch modes while either BingX engine still has a position open
    # under the mode being LEFT - the exchange-side position set is
    # completely different between VST and prod (same key, different
    # balance), so an in-flight switch would orphan a still-open, still-real
    # position from this dashboard's view of the world.
    open_symbols = (
        await shadow_repo.get_open_symbols(f"shadow_bingx_{current.mode}")
        + await momentum_repo.get_open_symbols(f"momentum_bingx_{current.mode}")
    )
    if open_symbols:
        raise HTTPException(
            status_code=409,
            detail=f"feche as posições abertas em modo {current.mode} antes de trocar de conta: {open_symbols}",
        )
    await bingx_account_repo.set_mode(body.mode)
    return {"mode": body.mode}


# -- market regime (Fase 17 - RegimeEngine) ------------------------------------
@router.get("/market/regime")
async def get_regime_summary(
    settings: Settings = Depends(get_settings_dep), candle_repo: CandleRepository = Depends(get_candle_repo),
) -> dict:
    """A feature, not a signal (same discipline as /market/derivatives and
    /market/liquidations) - computed on demand from the same candle history
    every trading engine already reads, never persisted, never fed into any
    strategy's gating logic here."""
    rows = []
    for symbol in settings.symbols:
        df = await candle_repo.fetch_ohlcv(symbol, _TRADING_INTERVAL, limit=250, closed_only=True)
        snapshot = compute_snapshot(symbol, _TRADING_INTERVAL, df)
        regime = classify_regime(snapshot)
        rows.append({
            "symbol": symbol, "trend_regime": regime.trend_regime, "volatility_regime": regime.volatility_regime,
            "adx_14": regime.adx_14, "volatility_percentile_100": regime.volatility_percentile_100,
        })
    return {"interval": _TRADING_INTERVAL, "symbols": rows}


# -- strategy enable/disable toggle (Fase 17f) ---------------------------------
@router.get("/settings/strategies")
async def get_strategy_settings(
    strategy_settings_repo: StrategySettingsRepository = Depends(get_strategy_settings_repo),
) -> dict:
    settings_by_id = await strategy_settings_repo.get_all_settings(ALL_STRATEGY_IDS)
    return {"strategies": [{"strategy_id": sid, "enabled": enabled} for sid, enabled in settings_by_id.items()]}


class StrategyToggleRequest(BaseModel):
    enabled: bool


@router.post("/settings/strategies/{strategy_id}")
async def set_strategy_enabled(
    strategy_id: str, body: StrategyToggleRequest,
    strategy_settings_repo: StrategySettingsRepository = Depends(get_strategy_settings_repo),
) -> dict:
    if strategy_id not in ALL_STRATEGY_IDS:
        raise HTTPException(status_code=404, detail=f"unknown strategy_id {strategy_id!r}")
    await strategy_settings_repo.set_enabled(strategy_id, body.enabled)
    return {"strategy_id": strategy_id, "enabled": body.enabled}


# -- capital allocation (Fase 17g - Shadow BingX / Momentum BingX split) ------
@router.get("/settings/capital-allocation")
async def get_capital_allocation(
    capital_allocation_repo: CapitalAllocationRepository = Depends(get_capital_allocation_repo),
) -> dict:
    return {"allocations": await capital_allocation_repo.get_all_allocations()}


class CapitalAllocationRequest(BaseModel):
    shadow_bingx_pct: float
    momentum_bingx_pct: float


@router.post("/settings/capital-allocation")
async def set_capital_allocation(
    body: CapitalAllocationRequest,
    capital_allocation_repo: CapitalAllocationRepository = Depends(get_capital_allocation_repo),
) -> dict:
    try:
        await capital_allocation_repo.set_allocations(body.shadow_bingx_pct, body.momentum_bingx_pct)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"allocations": await capital_allocation_repo.get_all_allocations()}


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
