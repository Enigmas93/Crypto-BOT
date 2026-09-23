#!/usr/bin/env python
"""Runs the Phase 10 Shadow Trading Engine forever: for every configured
symbol, polls the real candle history already collected (Phase 1/2/3),
reconciles any open real position against what Binance itself reports, or
evaluates the Strategy Engine for a new entry - gated by the Kill Switch
(Phase 7b) and the real RiskEngine (Phase 7), exactly like Paper Trading
(Phase 9), except every fill here is a REAL order on Binance Futures.

Two symbol buckets (Fase 13), same as Paper Trading: core_symbols on 1h,
speculative_symbol_list on a shorter interval (SPECULATIVE_INTERVAL). Same
generic order-placement code path for both - no symbol-specific branching -
but this is real orders (testnet money, still real exchange mechanics), so
watch the first live cycles for each new speculative symbol closely rather
than assuming it behaves identically to the already-validated BTCUSDT path.

Hard safety gate: refuses to run at all unless BINANCE_TESTNET=true and
LIVE_TRADING=false. This script places real orders - it must never do so
against a real-money account without a separate, deliberate decision to
change that gate (not just running this script).

Run scripts/verify_execution_setup.py and scripts/verify_shadow_trading.py
first to confirm credentials and the order-placement pipeline work before
leaving this running unattended.

Run the collector first (Phase 1/2) so there is real, continuously
updating candle history to act on:
    python scripts/run_collector.py         # in one terminal
    python scripts/run_shadow_trading.py     # in another

Usage (from backend/, venv active):
    python scripts/run_shadow_trading.py

Ctrl+C to stop.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aegis.config import get_settings  # noqa: E402
from aegis.db.candle_repository import CandleRepository  # noqa: E402
from aegis.db.engine import close_pool, create_pool  # noqa: E402
from aegis.db.kill_switch_repository import KillSwitchRepository  # noqa: E402
from aegis.db.risk_repository import RiskRepository  # noqa: E402
from aegis.db.shadow_repository import ShadowRepository  # noqa: E402
from aegis.execution.binance_provider import BinanceExecutionProvider  # noqa: E402
from aegis.logging_utils import configure_logging, get_logger, log_event  # noqa: E402
from aegis.notifications.telegram import TelegramNotifier  # noqa: E402
from aegis.providers.binance.rest_client import BinanceFuturesRestClient, BinanceRestError  # noqa: E402
from aegis.shadow.engine import ShadowTradingEngine  # noqa: E402
from aegis.shadow.models import ShadowTradingConfig  # noqa: E402

_LOG = get_logger("scripts.run_shadow_trading")
_CORE_INTERVAL = "1h"
_STARTING_EQUITY = 1000.0


async def _main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)

    if not settings.binance_testnet or settings.live_trading:
        log_event(
            _LOG, "refusing_to_run", level=40,
            message="Shadow Trading places real orders - it only ever runs against "
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
    candle_repo = CandleRepository(pool)
    shadow_repo = ShadowRepository(pool)
    risk_repo = RiskRepository(pool)
    notifier = TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id)
    kill_switch_repo = KillSwitchRepository(pool, notifier=notifier)
    execution = BinanceExecutionProvider(rest)
    engine = ShadowTradingEngine(candle_repo, shadow_repo, risk_repo, kill_switch_repo, execution, settings)

    account_id = "shadow"
    try:
        rules_by_symbol = await rest.get_symbol_rules()
        await risk_repo.initialize_account_state(account_id, starting_equity=_STARTING_EQUITY)

        configs = [
            ShadowTradingConfig(symbol=symbol, interval=_CORE_INTERVAL, account_id=account_id,
                                 candle_limit=settings.paper_trading_candle_limit)
            for symbol in settings.core_symbols
        ] + [
            ShadowTradingConfig(symbol=symbol, interval=settings.speculative_interval, account_id=account_id,
                                 candle_limit=settings.paper_trading_candle_limit)
            for symbol in settings.speculative_symbol_list
        ]
        log_event(
            _LOG, "startup", account_id=account_id,
            core_symbols=settings.core_symbols, core_interval=_CORE_INTERVAL,
            speculative_symbols=settings.speculative_symbol_list, speculative_interval=settings.speculative_interval,
            testnet=settings.binance_testnet, poll_interval=settings.paper_trading_poll_interval_seconds,
        )

        while True:
            for config in configs:
                symbol_rules = rules_by_symbol.get(config.symbol)
                if symbol_rules is None:
                    log_event(_LOG, "unknown_symbol_rules", level=30, symbol=config.symbol)
                    continue
                try:
                    result = await engine.run_once(config, symbol_rules)
                except BinanceRestError as exc:
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
                        f"\U0001f6a8 SHADOW TRADING - INTERVENÇÃO MANUAL NECESSÁRIA\n"
                        f"Símbolo: {config.symbol}\nUma posição real pode estar sem proteção.\n{result.get('error', '')}"
                    )
                else:
                    log_event(_LOG, "cycle", symbol=config.symbol, action=action, **result)
            await asyncio.sleep(settings.paper_trading_poll_interval_seconds)
    finally:
        await notifier.aclose()
        await rest.aclose()
        await close_pool(pool)


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
