#!/usr/bin/env python
"""Runs the Fase 16 BingX Momentum Engine forever - the BingX counterpart
to scripts/run_momentum_trading.py, same "moonshot" scanner, same liquid-
pairs-only floor, same everything except where the order is executed.

Scanning still reads Binance's real 24h ticker universe (`rest` below) -
the scanner ranks by real market momentum, which doesn't change depending
on which exchange later executes the trade. Only EXECUTION happens on
BingX, via BingXExecutionProvider, behind the exact same
MomentumTradingEngine every other exchange uses.

One real difference from the Binance version: Momentum's symbol universe
is DYNAMIC (the scanner's current top-N across the whole Binance exchange,
not a fixed pre-vetted list like Shadow Trading's 7 symbols) - a candidate
symbol is not guaranteed to also be listed on BingX. Symbol rules are
fetched from BingX's OWN contract list (needed for correct order sizing on
the exchange the order actually goes to) and any candidate BingX doesn't
list is skipped for this account (logged once, not spammy) rather than
guessed at.

Runs as account_id "momentum_bingx" - independent of Binance's "momentum"
account (separate risk_account_state/kill_switch/momentum_positions rows,
its own scan-then-trade cycle). The two never interfere with each other.

Hard safety gate: refuses to run at all unless BINGX_TESTNET=true and
LIVE_TRADING=false.

Run scripts/verify_bingx_execution_setup.py first to confirm credentials
and the account's position mode before leaving this running unattended.

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
from aegis.db.engine import close_pool, create_pool  # noqa: E402
from aegis.db.kill_switch_repository import KillSwitchRepository  # noqa: E402
from aegis.db.momentum_repository import MomentumRepository  # noqa: E402
from aegis.db.risk_repository import RiskRepository  # noqa: E402
from aegis.execution.bingx_provider import BingXExecutionProvider  # noqa: E402
from aegis.logging_utils import configure_logging, get_logger, log_event  # noqa: E402
from aegis.momentum.engine import MomentumTradingEngine  # noqa: E402
from aegis.momentum.models import MomentumConfig  # noqa: E402
from aegis.notifications.telegram import TelegramNotifier  # noqa: E402
from aegis.providers.bingx.rest_client import BingXFuturesRestClient, BingXRestError  # noqa: E402
from aegis.providers.binance.rest_client import BinanceFuturesRestClient, BinanceRestError  # noqa: E402

_LOG = get_logger("scripts.run_bingx_momentum_trading")
_STARTING_EQUITY = 1000.0
_ACCOUNT_ID = "momentum_bingx"


async def _main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)

    if not settings.bingx_testnet or settings.live_trading:
        log_event(
            _LOG, "refusing_to_run", level=40,
            message="BingX Momentum Engine places real orders - it only ever runs against "
                    "BINGX_TESTNET=true and LIVE_TRADING=false.",
            bingx_testnet=settings.bingx_testnet, live_trading=settings.live_trading,
        )
        return
    if not settings.bingx_api_key or not settings.bingx_api_secret:
        log_event(_LOG, "missing_credentials", level=40, message="Run scripts/verify_bingx_execution_setup.py first")
        return

    # Data/scanning stays on Binance (public, no credentials needed) -
    # only order execution goes through BingX.
    rest = BinanceFuturesRestClient(testnet=True)
    bingx_rest = BingXFuturesRestClient(
        testnet=True, api_key=settings.bingx_api_key, api_secret=settings.bingx_api_secret,
    )
    pool = await create_pool(settings)
    momentum_repo = MomentumRepository(pool)
    risk_repo = RiskRepository(pool)
    notifier = TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id)
    kill_switch_repo = KillSwitchRepository(pool, notifier=notifier)
    execution = BingXExecutionProvider(bingx_rest)
    engine = MomentumTradingEngine(rest, momentum_repo, risk_repo, kill_switch_repo, execution, settings)

    config = MomentumConfig(
        interval=settings.momentum_interval, account_id=_ACCOUNT_ID,
        min_quote_volume=settings.momentum_min_quote_volume, top_n=settings.momentum_top_n,
        trailing_callback_rate_pct=settings.momentum_trailing_callback_rate_pct,
        trailing_activation_pct=settings.momentum_trailing_activation_pct,
    )

    try:
        if await bingx_rest.get_position_mode():
            log_event(
                _LOG, "refusing_to_run", level=40,
                message="BingX account is in Hedge mode - run scripts/verify_bingx_execution_setup.py "
                        "first to switch it to one-way mode (only possible while the account is flat).",
            )
            return

        await risk_repo.initialize_account_state(_ACCOUNT_ID, starting_equity=_STARTING_EQUITY)
        log_event(
            _LOG, "startup", account_id=_ACCOUNT_ID, interval=config.interval,
            min_quote_volume=config.min_quote_volume, top_n=config.top_n,
            scan_interval=settings.momentum_scan_interval_seconds,
            poll_interval=settings.momentum_poll_interval_seconds, testnet=settings.bingx_testnet,
        )

        candidate_symbols: dict[str, float] = {}  # symbol -> momentum_score
        last_scan = 0.0
        warned_unlisted: set[str] = set()  # avoid re-logging the same BingX-unlisted symbol every cycle

        while True:
            now = time.monotonic()
            if now - last_scan >= settings.momentum_scan_interval_seconds:
                try:
                    open_symbols = set(await momentum_repo.get_open_symbols(_ACCOUNT_ID))
                    candidates = await engine.scan(config, exclude=open_symbols)
                except BinanceRestError as exc:
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

            open_symbols = set(await momentum_repo.get_open_symbols(_ACCOUNT_ID))
            symbols_to_process = open_symbols | set(candidate_symbols)

            if symbols_to_process:
                try:
                    rules_by_symbol = await bingx_rest.get_symbol_rules()
                except BingXRestError as exc:
                    log_event(_LOG, "transient_network_error", level=30, stage="get_symbol_rules", error=str(exc))
                    rules_by_symbol = {}
                for symbol in symbols_to_process:
                    symbol_rules = rules_by_symbol.get(symbol)
                    if symbol_rules is None:
                        if rules_by_symbol and symbol not in warned_unlisted:
                            # A genuine, expected case for a dynamic universe - not
                            # every symbol Binance's scanner surfaces is also listed
                            # on BingX. Logged once per symbol, not every cycle.
                            log_event(_LOG, "symbol_not_listed_on_bingx", level=30, symbol=symbol)
                            warned_unlisted.add(symbol)
                        continue
                    momentum_score = candidate_symbols.get(symbol, 0.0)
                    try:
                        result = await engine.run_once_for_symbol(config, symbol, symbol_rules, momentum_score)
                    except (BinanceRestError, BingXRestError) as exc:
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
                            f"\U0001f6a8 BINGX MOMENTUM TRADING - INTERVENÇÃO MANUAL NECESSÁRIA\n"
                            f"Símbolo: {symbol}\nUma posição real (BingX VST) pode estar sem proteção.\n"
                            f"{result.get('error', '')}"
                        )
                    else:
                        log_event(_LOG, "cycle", symbol=symbol, action=action, **result)

            await asyncio.sleep(settings.momentum_poll_interval_seconds)
    finally:
        await notifier.aclose()
        await rest.aclose()
        await bingx_rest.aclose()
        await close_pool(pool)


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
