#!/usr/bin/env python
"""Runs the Fase 16 BingX Shadow Trading Engine forever - the BingX
counterpart to scripts/run_shadow_trading.py, and the second stage of the
same VST -> Shadow -> Paper -> Live funnel already used for Binance.

Market data is still Binance's (public, free, already deeply tested) - only
EXECUTION happens on BingX, via BingXExecutionProvider, behind the exact
same ShadowTradingEngine every other exchange uses. This is the
architectural point of the whole migration: nothing about the strategy,
confluence or risk pipeline changes, only which exchange places the order.

Runs as a SEPARATE account_id ("shadow_bingx") from Binance's own "shadow"
account, in its own risk_account_state / kill_switch / shadow_positions
rows - the two engines are independent and can run side by side without
interfering with each other, exactly as intended ("continua rodando o que
já funciona, só adiciona a BingX ao lado").

Hard safety gate: refuses to run at all unless BINGX_TESTNET=true and
LIVE_TRADING=false. This script places real orders (VST/testnet only for
now) - it must never do so against a real-money account without a
separate, deliberate decision to change that gate.

Run scripts/verify_bingx_execution_setup.py and
scripts/verify_bingx_shadow_trading.py first to confirm credentials, the
account's position mode, and the order-placement pipeline work before
leaving this running unattended.

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
from aegis.db.candle_repository import CandleRepository  # noqa: E402
from aegis.db.engine import close_pool, create_pool  # noqa: E402
from aegis.db.kill_switch_repository import KillSwitchRepository  # noqa: E402
from aegis.db.risk_repository import RiskRepository  # noqa: E402
from aegis.db.shadow_repository import ShadowRepository  # noqa: E402
from aegis.execution.bingx_provider import BingXExecutionProvider  # noqa: E402
from aegis.logging_utils import configure_logging, get_logger, log_event  # noqa: E402
from aegis.notifications.telegram import TelegramNotifier  # noqa: E402
from aegis.providers.bingx.rest_client import BingXFuturesRestClient, BingXRestError  # noqa: E402
from aegis.shadow.engine import ShadowTradingEngine  # noqa: E402
from aegis.shadow.models import ShadowTradingConfig  # noqa: E402

_LOG = get_logger("scripts.run_bingx_shadow_trading")
_CORE_INTERVAL = "1h"
_STARTING_EQUITY = 1000.0
_ACCOUNT_ID = "shadow_bingx"


async def _main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)

    if not settings.bingx_testnet or settings.live_trading:
        log_event(
            _LOG, "refusing_to_run", level=40,
            message="BingX Shadow Trading places real orders - it only ever runs against "
                    "BINGX_TESTNET=true and LIVE_TRADING=false.",
            bingx_testnet=settings.bingx_testnet, live_trading=settings.live_trading,
        )
        return
    if not settings.bingx_api_key or not settings.bingx_api_secret:
        log_event(_LOG, "missing_credentials", level=40, message="Run scripts/verify_bingx_execution_setup.py first")
        return

    rest = BingXFuturesRestClient(
        testnet=True, api_key=settings.bingx_api_key, api_secret=settings.bingx_api_secret,
    )
    pool = await create_pool(settings)
    candle_repo = CandleRepository(pool)
    shadow_repo = ShadowRepository(pool)
    risk_repo = RiskRepository(pool)
    notifier = TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id)
    kill_switch_repo = KillSwitchRepository(pool, notifier=notifier)
    execution = BingXExecutionProvider(rest)
    engine = ShadowTradingEngine(candle_repo, shadow_repo, risk_repo, kill_switch_repo, execution, settings)

    try:
        if await rest.get_position_mode():
            log_event(
                _LOG, "refusing_to_run", level=40,
                message="BingX account is in Hedge mode - run scripts/verify_bingx_execution_setup.py "
                        "first to switch it to one-way mode (only possible while the account is flat).",
            )
            return

        rules_by_symbol = await rest.get_symbol_rules()
        await risk_repo.initialize_account_state(_ACCOUNT_ID, starting_equity=_STARTING_EQUITY)

        configs = [
            ShadowTradingConfig(symbol=symbol, interval=_CORE_INTERVAL, account_id=_ACCOUNT_ID,
                                 candle_limit=settings.paper_trading_candle_limit)
            for symbol in settings.core_symbols
        ] + [
            ShadowTradingConfig(symbol=symbol, interval=settings.speculative_interval, account_id=_ACCOUNT_ID,
                                 candle_limit=settings.paper_trading_candle_limit)
            for symbol in settings.speculative_symbol_list
        ]
        log_event(
            _LOG, "startup", account_id=_ACCOUNT_ID,
            core_symbols=settings.core_symbols, core_interval=_CORE_INTERVAL,
            speculative_symbols=settings.speculative_symbol_list, speculative_interval=settings.speculative_interval,
            testnet=settings.bingx_testnet, poll_interval=settings.paper_trading_poll_interval_seconds,
        )

        while True:
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
                        f"\U0001f6a8 BINGX SHADOW TRADING - INTERVENÇÃO MANUAL NECESSÁRIA\n"
                        f"Símbolo: {config.symbol}\nUma posição real (BingX VST) pode estar sem proteção.\n"
                        f"{result.get('error', '')}"
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
