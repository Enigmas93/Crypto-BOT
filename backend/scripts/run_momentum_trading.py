#!/usr/bin/env python
"""Runs the Phase 14 Momentum Engine forever - the "moonshot" scanner,
explicitly scoped to LIQUID pairs only (see aegis/momentum/engine.py's
module docstring for why: the user was asked and chose this over including
thin/new listings, given the much higher risk of the latter).

Every MOMENTUM_SCAN_INTERVAL_SECONDS, re-ranks the real 24h Binance
universe by momentum (aegis.scanner.ranking) among symbols meeting the
liquidity floor. Every MOMENTUM_POLL_INTERVAL_SECONDS (much more often),
reconciles any open position (even one whose symbol just dropped out of
the current top-N - a position already open is managed until it closes,
never abandoned) and evaluates new entries for the current candidates.

Real orders on Binance Futures - same hard safety gate as Shadow Trading:
refuses to run at all unless BINANCE_TESTNET=true and LIVE_TRADING=false.

Candles come directly from REST each cycle (get_klines), not the
collector's persisted table - the symbol universe here is dynamic, unlike
every other engine's fixed collector_symbols list. No collector
dependency for this script.

Usage (from backend/, venv active):
    python scripts/run_momentum_trading.py

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
from aegis.db.engine import close_pool, create_pool  # noqa: E402
from aegis.db.kill_switch_repository import KillSwitchRepository  # noqa: E402
from aegis.db.momentum_repository import MomentumRepository  # noqa: E402
from aegis.db.risk_repository import RiskRepository  # noqa: E402
from aegis.db.strategy_settings_repository import StrategySettingsRepository  # noqa: E402
from aegis.execution.binance_provider import BinanceExecutionProvider  # noqa: E402
from aegis.logging_utils import configure_logging, get_logger, log_event  # noqa: E402
from aegis.momentum.engine import MomentumTradingEngine  # noqa: E402
from aegis.momentum.models import MomentumConfig  # noqa: E402
from aegis.notifications.telegram import TelegramNotifier  # noqa: E402
from aegis.providers.binance.rest_client import BinanceFuturesRestClient, BinanceRestError  # noqa: E402

_LOG = get_logger("scripts.run_momentum_trading")
_STARTING_EQUITY = 1000.0


async def _main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)

    if not settings.binance_testnet or settings.live_trading:
        log_event(
            _LOG, "refusing_to_run", level=40,
            message="Momentum Engine places real orders - it only ever runs against "
                    "BINANCE_TESTNET=true and LIVE_TRADING=false.",
            binance_testnet=settings.binance_testnet, live_trading=settings.live_trading,
        )
        return
    if not settings.binance_api_key or not settings.binance_api_secret:
        log_event(_LOG, "missing_credentials", level=40, message="Run scripts/verify_execution_setup.py first")
        return

    rest = BinanceFuturesRestClient(
        testnet=True, api_key=settings.binance_api_key, api_secret=settings.binance_api_secret,
    )
    pool = await create_pool(settings)
    momentum_repo = MomentumRepository(pool)
    risk_repo = RiskRepository(pool)
    strategy_settings_repo = StrategySettingsRepository(pool)
    notifier = TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id)
    kill_switch_repo = KillSwitchRepository(pool, notifier=notifier)
    execution = BinanceExecutionProvider(rest)
    engine = MomentumTradingEngine(rest, momentum_repo, risk_repo, kill_switch_repo, execution, settings,
                                    strategy_settings_repo=strategy_settings_repo)

    account_id = settings.momentum_account_id
    config = MomentumConfig(
        interval=settings.momentum_interval, account_id=account_id,
        min_quote_volume=settings.momentum_min_quote_volume, top_n=settings.momentum_top_n,
        trailing_activation_atr_multiple=settings.momentum_trailing_activation_atr_multiple,
        trailing_callback_atr_multiple=settings.momentum_trailing_callback_atr_multiple,
        trailing_activation_min_pct=settings.momentum_trailing_activation_min_pct,
        trailing_activation_max_pct=settings.momentum_trailing_activation_max_pct,
        trailing_callback_min_pct=settings.momentum_trailing_callback_min_pct,
        trailing_callback_max_pct=settings.momentum_trailing_callback_max_pct,
    )

    try:
        await risk_repo.initialize_account_state(account_id, starting_equity=_STARTING_EQUITY)
        log_event(
            _LOG, "startup", account_id=account_id, interval=config.interval,
            min_quote_volume=config.min_quote_volume, top_n=config.top_n,
            scan_interval=settings.momentum_scan_interval_seconds,
            poll_interval=settings.momentum_poll_interval_seconds, testnet=settings.binance_testnet,
        )

        candidate_symbols: dict[str, float] = {}  # symbol -> momentum_score
        last_scan = 0.0

        while True:
            now = time.monotonic()
            if now - last_scan >= settings.momentum_scan_interval_seconds:
                try:
                    open_symbols = set(await momentum_repo.get_open_symbols(account_id))
                    candidates = await engine.scan(config, exclude=open_symbols)
                except BinanceRestError as exc:
                    # Leave last_scan untouched so the next poll cycle
                    # retries the scan soon, instead of waiting a full
                    # scan_interval after a transient network blip.
                    log_event(_LOG, "transient_network_error", level=30, stage="scan", error=str(exc))
                else:
                    candidate_symbols = {c.symbol: c.momentum_score for c in candidates}
                    last_scan = now
                    # Persisted so the dashboard can show what the scanner
                    # currently sees, even between entries - previously only
                    # visible in this process's own logs.
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
                    rules_by_symbol = await rest.get_symbol_rules()
                except BinanceRestError as exc:
                    log_event(_LOG, "transient_network_error", level=30, stage="get_symbol_rules", error=str(exc))
                    rules_by_symbol = {}
                for symbol in symbols_to_process:
                    symbol_rules = rules_by_symbol.get(symbol)
                    if symbol_rules is None:
                        if rules_by_symbol:  # only warn if this symbol is genuinely unknown, not a fetch failure
                            log_event(_LOG, "unknown_symbol_rules", level=30, symbol=symbol)
                        continue
                    momentum_score = candidate_symbols.get(symbol, 0.0)
                    try:
                        result = await engine.run_once_for_symbol(config, symbol, symbol_rules, momentum_score)
                    except BinanceRestError as exc:
                        # A transient network/DNS blip must not kill the
                        # whole process - real stop/trailing orders already
                        # on the exchange keep protecting any open position
                        # regardless; this symbol is simply retried next cycle.
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
                            f"\U0001f6a8 MOMENTUM TRADING - INTERVENÇÃO MANUAL NECESSÁRIA\n"
                            f"Símbolo: {symbol}\nUma posição real pode estar sem proteção.\n{result.get('error', '')}"
                        )
                    else:
                        log_event(_LOG, "cycle", symbol=symbol, action=action, **result)

            await asyncio.sleep(settings.momentum_poll_interval_seconds)
    finally:
        await notifier.aclose()
        await rest.aclose()
        await close_pool(pool)


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
