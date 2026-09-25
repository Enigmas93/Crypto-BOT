#!/usr/bin/env python
"""Runs the BingX Shadow Trading Engine forever - the BingX counterpart to
scripts/run_shadow_trading.py, and the second stage of the same
VST -> Shadow -> Paper -> Live funnel already used for Binance.

Market data is still Binance's (public, free, already deeply tested) - only
EXECUTION happens on BingX, via BingXExecutionProvider, behind the exact
same ShadowTradingEngine every other exchange uses. This is the
architectural point of the whole migration: nothing about the strategy,
confluence or risk pipeline changes, only which exchange places the order.

Fase 17: the BingX API key and demo/live mode are no longer read from
.env - they're managed from the dashboard (Configurações > BingX) and
picked up here via `BingxSessionManager`, which polls
`bingx_account_settings` once per cycle and transparently rebuilds the
REST client/execution provider when the user switches modes, with NO
process restart needed. Demo and live trade history/risk state are kept in
completely separate account_ids ("shadow_bingx_demo" / "shadow_bingx_live")
precisely because switching modes points at a different BingX balance - an
in-flight position from one mode is invisible (not "closed", genuinely
untracked) from the other, so the dashboard's mode-switch endpoint refuses
to switch while either account still has an open position (see
aegis.api.routes.set_bingx_mode).

Run scripts/verify_bingx_execution_setup.py first to confirm the account's
position mode before leaving this running unattended - it still refuses to
run in Hedge mode regardless of which balance (demo/live) is active.

Usage (from backend/, venv active):
    python scripts/run_bingx_shadow_trading.py

Ctrl+C to stop.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aegis.config import get_settings  # noqa: E402
from aegis.db.bingx_account_repository import BingxAccountRepository  # noqa: E402
from aegis.db.candle_repository import CandleRepository  # noqa: E402
from aegis.db.capital_allocation_repository import CapitalAllocationRepository  # noqa: E402
from aegis.db.engine import close_pool, create_pool  # noqa: E402
from aegis.db.kill_switch_repository import KillSwitchRepository  # noqa: E402
from aegis.db.risk_repository import RiskRepository  # noqa: E402
from aegis.db.shadow_repository import ShadowRepository  # noqa: E402
from aegis.db.strategy_settings_repository import StrategySettingsRepository  # noqa: E402
from aegis.execution.bingx_session import BingxCredentialsNotConfigured, BingxSessionManager  # noqa: E402
from aegis.logging_utils import configure_logging, get_logger, log_event  # noqa: E402
from aegis.notifications.telegram import TelegramNotifier  # noqa: E402
from aegis.providers.bingx.rest_client import BingXRestError  # noqa: E402
from aegis.shadow.engine import ShadowTradingEngine  # noqa: E402
from aegis.shadow.models import ShadowTradingConfig  # noqa: E402

_LOG = get_logger("scripts.run_bingx_shadow_trading")
_CORE_INTERVAL = "1h"
_ACCOUNT_SUFFIX = "shadow_bingx"


def _build_configs(account_id: str, settings) -> list[ShadowTradingConfig]:
    return [
        ShadowTradingConfig(symbol=symbol, interval=_CORE_INTERVAL, account_id=account_id,
                             candle_limit=settings.paper_trading_candle_limit)
        for symbol in settings.core_symbols
    ] + [
        ShadowTradingConfig(symbol=symbol, interval=settings.speculative_interval, account_id=account_id,
                             candle_limit=settings.paper_trading_candle_limit)
        for symbol in settings.speculative_symbol_list
    ]


async def _main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)

    pool = await create_pool(settings)
    candle_repo = CandleRepository(pool)
    shadow_repo = ShadowRepository(pool)
    risk_repo = RiskRepository(pool)
    strategy_settings_repo = StrategySettingsRepository(pool)
    capital_allocation_repo = CapitalAllocationRepository(pool)
    notifier = TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id)
    kill_switch_repo = KillSwitchRepository(pool, notifier=notifier)
    account_repo = BingxAccountRepository(pool, settings.credential_encryption_key)
    session_manager = BingxSessionManager(account_repo, _ACCOUNT_SUFFIX, "scripts.run_bingx_shadow_trading")

    try:
        while True:
            try:
                session = await session_manager.refresh()
            except BingxCredentialsNotConfigured as exc:
                log_event(_LOG, "waiting_for_credentials", level=30, message=str(exc))
                await asyncio.sleep(settings.paper_trading_poll_interval_seconds)
                continue

            account_id = session.account_id
            try:
                if await session.rest.get_position_mode():
                    log_event(
                        _LOG, "refusing_to_run", level=40, account_id=account_id,
                        message="BingX account is in Hedge mode - run scripts/verify_bingx_execution_setup.py "
                                "first to switch it to one-way mode (only possible while the account is flat).",
                    )
                    await asyncio.sleep(settings.paper_trading_poll_interval_seconds)
                    continue
                rules_by_symbol = await session.rest.get_symbol_rules()
                # Real starting balance, not a hardcoded guess (Fase 17b) -
                # only matters the very first time this account_id is ever
                # initialized (initialize_account_state no-ops afterward),
                # but a wrong seed here would misprice every DAILY_LOSS_LIMIT
                # check until the next daily reset. Whatever the user has
                # actually deposited (e.g. $100 real money) becomes the
                # sizing baseline, not an assumed $1000.
                starting_equity = await session.execution.get_equity()
            except BingXRestError as exc:
                log_event(_LOG, "transient_network_error", level=30, stage="startup_checks", error=str(exc))
                await asyncio.sleep(settings.paper_trading_poll_interval_seconds)
                continue

            await risk_repo.initialize_account_state(account_id, starting_equity=starting_equity)
            engine = ShadowTradingEngine(candle_repo, shadow_repo, risk_repo, kill_switch_repo, session.execution, settings,
                                          strategy_settings_repo=strategy_settings_repo,
                                          capital_allocation_repo=capital_allocation_repo,
                                          capital_allocation_key="shadow_bingx")
            configs = _build_configs(account_id, settings)
            log_event(
                _LOG, "session_started", account_id=account_id, mode=session.mode,
                core_symbols=settings.core_symbols, core_interval=_CORE_INTERVAL,
                speculative_symbols=settings.speculative_symbol_list, speculative_interval=settings.speculative_interval,
                poll_interval=settings.paper_trading_poll_interval_seconds,
            )

            # Inner loop: keep trading under this session until the active
            # mode changes underneath us, then fall back out to rebuild
            # everything (engine, configs, REST client) against the new one.
            while True:
                current = await session_manager.refresh()
                if current.mode != session.mode:
                    break
                try:
                    await engine.sync_equity(account_id)
                except BingXRestError as exc:
                    # Sizing simply uses whatever equity was last synced
                    # (or the initial seed, on the very first cycle) - a
                    # transient balance-fetch failure must not block the
                    # whole cycle's exit checks on already-open positions.
                    log_event(_LOG, "transient_network_error", level=30, stage="sync_equity", error=str(exc))
                for config in configs:
                    symbol_rules = rules_by_symbol.get(config.symbol)
                    if symbol_rules is None:
                        log_event(_LOG, "unknown_symbol_rules", level=30, symbol=config.symbol)
                        continue
                    try:
                        result = await engine.run_once(config, symbol_rules)
                    except BingXRestError as exc:
                        # A transient network/DNS blip must not kill the whole
                        # process - real stop/TP orders already on the exchange
                        # keep protecting any open position regardless; this
                        # symbol is simply retried next cycle.
                        log_event(_LOG, "transient_network_error", level=30, symbol=config.symbol, error=str(exc))
                        continue
                    action = result.pop("action")
                    if action == "POSITION_CLOSED":
                        trade = result.pop("trade")
                        log_event(
                            _LOG, "position_closed", symbol=config.symbol, side=trade.side,
                            exit_reason=trade.exit_reason, net_pnl=round(trade.net_pnl, 2),
                            r_multiple=round(trade.r_multiple, 3), **result,
                        )
                    elif action == "NO_NEW_CANDLE":
                        pass  # expected every cycle between candle closes
                    elif action == "BRACKET_FAILED" and not result.get("flattened", True):
                        log_event(_LOG, "MANUAL_INTERVENTION_REQUIRED", level=50, symbol=config.symbol, **result)
                        await notifier.send(
                            f"\U0001f6a8 BINGX SHADOW TRADING ({session.mode.upper()}) - INTERVENÇÃO MANUAL NECESSÁRIA\n"
                            f"Símbolo: {config.symbol}\nUma posição real pode estar sem proteção.\n"
                            f"{result.get('error', '')}"
                        )
                    else:
                        log_event(_LOG, "cycle", symbol=config.symbol, action=action, **result)
                await asyncio.sleep(settings.paper_trading_poll_interval_seconds)
    finally:
        await notifier.aclose()
        await session_manager.aclose()
        await close_pool(pool)


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
