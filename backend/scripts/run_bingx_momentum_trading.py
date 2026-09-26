#!/usr/bin/env python
"""Runs the BingX Momentum Engine forever - the BingX counterpart to
scripts/run_momentum_trading.py, same "moonshot" scanner logic and
liquid-pairs-only floor, same everything except where the order is
executed AND where the scanner looks for candidates.

Scanning and candle data come from BingX's OWN market (`session.rest`
below serves both), NOT Binance's - found live (2026-09-24) that scanning
Binance's ticker universe to trade on BingX produces real symbol
mismatches: a candidate Binance's scanner ranks highly is not guaranteed
to exist on BingX at all (BROCCOLI714USDT was a confirmed real example),
and even when a symbol IS listed on both, computing technical indicators
from the wrong exchange's candles would be misleading. This account scans,
reads candles, and sizes orders entirely against BingX's own contract
list, ticker feed, and klines.

Fase 17: the BingX API key and demo/live mode are no longer read from
.env - they're managed from the dashboard (Configurações > BingX) and
picked up here via `BingxSessionManager`, which polls
`bingx_account_settings` and transparently rebuilds the REST client when
the user switches modes, no process restart needed. Demo and live history
live in separate account_ids ("momentum_bingx_demo" / "momentum_bingx_live")
- see run_bingx_shadow_trading.py's module docstring for why switching is
blocked while either still has an open position.

Run scripts/verify_bingx_execution_setup.py first to confirm the account's
position mode before leaving this running unattended.

Usage (from backend/, venv active):
    python scripts/run_bingx_momentum_trading.py

Ctrl+C to stop.
"""
from __future__ import annotations

import asyncio
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aegis.config import get_settings  # noqa: E402
from aegis.db.bingx_account_repository import BingxAccountRepository  # noqa: E402
from aegis.db.capital_allocation_repository import CapitalAllocationRepository  # noqa: E402
from aegis.db.engine import close_pool, create_pool  # noqa: E402
from aegis.db.kill_switch_repository import KillSwitchRepository  # noqa: E402
from aegis.db.momentum_repository import MomentumRepository  # noqa: E402
from aegis.db.risk_repository import RiskRepository  # noqa: E402
from aegis.db.strategy_settings_repository import StrategySettingsRepository  # noqa: E402
from aegis.execution.bingx_session import BingxCredentialsNotConfigured, BingxSessionManager  # noqa: E402
from aegis.logging_utils import configure_logging, get_logger, log_event  # noqa: E402
from aegis.momentum.engine import MomentumTradingEngine  # noqa: E402
from aegis.momentum.models import MomentumConfig  # noqa: E402
from aegis.notifications.telegram import TelegramNotifier  # noqa: E402
from aegis.providers.bingx.rest_client import BingXRestError  # noqa: E402

_LOG = get_logger("scripts.run_bingx_momentum_trading")
_ACCOUNT_SUFFIX = "momentum_bingx"


def _build_config(account_id: str, settings) -> MomentumConfig:
    return MomentumConfig(
        interval=settings.momentum_interval, account_id=account_id,
        min_quote_volume=settings.bingx_momentum_min_quote_volume, top_n=settings.momentum_top_n,
        trailing_activation_atr_multiple=settings.momentum_trailing_activation_atr_multiple,
        trailing_callback_atr_multiple=settings.momentum_trailing_callback_atr_multiple,
        trailing_activation_min_pct=settings.momentum_trailing_activation_min_pct,
        trailing_activation_max_pct=settings.momentum_trailing_activation_max_pct,
        trailing_callback_min_pct=settings.momentum_trailing_callback_min_pct,
        trailing_callback_max_pct=settings.momentum_trailing_callback_max_pct,
    )


async def _main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)

    pool = await create_pool(settings)
    momentum_repo = MomentumRepository(pool)
    risk_repo = RiskRepository(pool)
    strategy_settings_repo = StrategySettingsRepository(pool)
    capital_allocation_repo = CapitalAllocationRepository(pool)
    notifier = TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id)
    kill_switch_repo = KillSwitchRepository(pool, notifier=notifier)
    account_repo = BingxAccountRepository(pool, settings.credential_encryption_key)
    session_manager = BingxSessionManager(account_repo, _ACCOUNT_SUFFIX, "scripts.run_bingx_momentum_trading")

    try:
        while True:
            try:
                session = await session_manager.refresh()
            except BingxCredentialsNotConfigured as exc:
                log_event(_LOG, "waiting_for_credentials", level=30, message=str(exc))
                await asyncio.sleep(settings.momentum_poll_interval_seconds)
                continue

            account_id = session.account_id
            try:
                if await session.rest.get_position_mode():
                    log_event(
                        _LOG, "refusing_to_run", level=40, account_id=account_id,
                        message="BingX account is in Hedge mode - run scripts/verify_bingx_execution_setup.py "
                                "first to switch it to one-way mode (only possible while the account is flat).",
                    )
                    await asyncio.sleep(settings.momentum_poll_interval_seconds)
                    continue
                # Real starting balance, not a hardcoded guess (Fase 17b) -
                # see run_bingx_shadow_trading.py's identical comment.
                starting_equity = await session.execution.get_equity()
            except BingXRestError as exc:
                log_event(_LOG, "transient_network_error", level=30, stage="startup_checks", error=str(exc))
                await asyncio.sleep(settings.momentum_poll_interval_seconds)
                continue

            engine = MomentumTradingEngine(session.rest, momentum_repo, risk_repo, kill_switch_repo, session.execution, settings,
                                            strategy_settings_repo=strategy_settings_repo,
                                            capital_allocation_repo=capital_allocation_repo,
                                            capital_allocation_key="momentum_bingx")
            config = _build_config(account_id, settings)

            await risk_repo.initialize_account_state(account_id, starting_equity=starting_equity)
            log_event(
                _LOG, "session_started", account_id=account_id, mode=session.mode, interval=config.interval,
                min_quote_volume=config.min_quote_volume, top_n=config.top_n,
                scan_interval=settings.momentum_scan_interval_seconds,
                poll_interval=settings.momentum_poll_interval_seconds, data_source="bingx",
            )

            candidate_symbols: dict[str, float] = {}  # symbol -> momentum_score
            last_scan = 0.0
            warned_unlisted: set[str] = set()  # defensive only now - see run loop comment

            # Inner loop: keep trading under this session until the active
            # mode changes underneath us, then fall back out to rebuild.
            while True:
                current = await session_manager.refresh()
                if current.mode != session.mode:
                    break
                try:
                    await engine.sync_equity(account_id)
                except BingXRestError as exc:
                    log_event(_LOG, "transient_network_error", level=30, stage="sync_equity", error=str(exc))

                now = time.monotonic()
                if now - last_scan >= settings.momentum_scan_interval_seconds:
                    try:
                        open_symbols = set(await momentum_repo.get_open_symbols(account_id))
                        candidates = await engine.scan(config, exclude=open_symbols)
                    except BingXRestError as exc:
                        log_event(_LOG, "transient_network_error", level=30, stage="scan", error=str(exc))
                    else:
                        candidate_symbols = {c.symbol: c.momentum_score for c in candidates}
                        last_scan = now
                        await momentum_repo.save_scan_results(candidates, scanned_at=datetime.now(UTC))
                        log_event(
                            _LOG, "scan_complete",
                            candidates=[{"symbol": c.symbol, "momentum_score": round(c.momentum_score, 2),
                                         "price_change_pct": round(c.price_change_pct, 2)} for c in candidates],
                        )

                open_symbols = set(await momentum_repo.get_open_symbols(account_id))
                symbols_to_process = open_symbols | set(candidate_symbols)

                if symbols_to_process:
                    try:
                        rules_by_symbol = await session.rest.get_symbol_rules()
                    except BingXRestError as exc:
                        log_event(_LOG, "transient_network_error", level=30, stage="get_symbol_rules", error=str(exc))
                        rules_by_symbol = {}
                    for symbol in symbols_to_process:
                        symbol_rules = rules_by_symbol.get(symbol)
                        if symbol_rules is None:
                            if rules_by_symbol and symbol not in warned_unlisted:
                                log_event(_LOG, "symbol_missing_from_contract_list", level=30, symbol=symbol)
                                warned_unlisted.add(symbol)
                            continue
                        momentum_score = candidate_symbols.get(symbol, 0.0)
                        try:
                            result = await engine.run_once_for_symbol(config, symbol, symbol_rules, momentum_score)
                        except BingXRestError as exc:
                            log_event(_LOG, "transient_network_error", level=30, stage="run_once_for_symbol",
                                      symbol=symbol, error=str(exc))
                            continue
                        action = result.pop("action")
                        if action == "POSITION_CLOSED":
                            trade = result.pop("trade")
                            log_event(
                                _LOG, "position_closed", symbol=symbol, side=trade.side,
                                exit_reason=trade.exit_reason, net_pnl=round(trade.net_pnl, 2),
                                r_multiple=round(trade.r_multiple, 3), momentum_score=round(trade.momentum_score, 2),
                                **result,
                            )
                        elif action == "NO_NEW_CANDLE":
                            pass
                        elif action == "BRACKET_FAILED" and not result.get("flattened", True):
                            log_event(_LOG, "MANUAL_INTERVENTION_REQUIRED", level=50, symbol=symbol, **result)
                            await notifier.send(
                                f"\U0001f6a8 BINGX MOMENTUM TRADING ({session.mode.upper()}) - "
                                f"INTERVENÇÃO MANUAL NECESSÁRIA\n"
                                f"Símbolo: {symbol}\nUma posição real pode estar sem proteção.\n"
                                f"{result.get('error', '')}"
                            )
                        else:
                            log_event(_LOG, "cycle", symbol=symbol, action=action, **result)

                await asyncio.sleep(settings.momentum_poll_interval_seconds)
    finally:
        await notifier.aclose()
        await session_manager.aclose()
        await close_pool(pool)


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
