#!/usr/bin/env python
"""ONE-SHOT, manual live validation of real order placement on BingX
(Fase 16) - mirrors scripts/verify_shadow_trading.py. Run this AFTER
scripts/verify_bingx_execution_setup.py confirms clean, and BEFORE ever
starting the continuous poller (run_bingx_shadow_trading.py).

Places one small real bracket position on BingX VST (testnet), verifies
every piece of it against what the exchange itself reports, then
immediately undoes it (cancels the protective orders, closes the position)
rather than leaving anything open unattended.

Hard safety gate: refuses to run at all unless BINGX_TESTNET=true - this
script must never place a real-money order, on principle, regardless of
what the rest of the config says.

Usage (from backend/, venv active):
    python scripts/verify_bingx_shadow_trading.py
"""
from __future__ import annotations

import asyncio
import sys
from decimal import ROUND_DOWN, Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aegis.config import get_settings  # noqa: E402
from aegis.execution.bingx_provider import BingXExecutionProvider  # noqa: E402
from aegis.logging_utils import configure_logging, get_logger, log_event  # noqa: E402
from aegis.providers.bingx.rest_client import BingXFuturesRestClient  # noqa: E402
from aegis.providers.binance.rest_client import BinanceFuturesRestClient  # noqa: E402

_LOG = get_logger("scripts.verify_bingx_shadow_trading")
_SYMBOL = "BTCUSDT"
_LEVERAGE = 3


async def _main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)

    if not settings.bingx_testnet:
        log_event(
            _LOG, "refusing_to_run", level=40,
            message="This script only ever runs against BINGX_TESTNET=true - it places a real order "
                    "and must never do so against a real-money account.",
        )
        return
    if not settings.bingx_api_key or not settings.bingx_api_secret:
        log_event(_LOG, "missing_credentials", level=40, message="Run scripts/verify_bingx_execution_setup.py first")
        return

    rest = BingXFuturesRestClient(testnet=True, api_key=settings.bingx_api_key, api_secret=settings.bingx_api_secret)
    execution = BingXExecutionProvider(rest)

    try:
        if await rest.get_position_mode():
            log_event(
                _LOG, "refusing_to_run", level=40,
                message="Account is in Hedge mode - run scripts/verify_bingx_execution_setup.py first "
                        "to switch it to one-way mode.",
            )
            return

        rules = await rest.get_symbol_rules()
        btc_rules = rules[_SYMBOL]
        # BingX's public market-data client isn't built (Binance remains
        # the data source, by design - see this project's Fase 16 plan) -
        # a real, LIVE reference price is fetched straight from Binance's
        # already-built public REST client instead of a hardcoded guess.
        # An earlier version of this script hardcoded 65000.0 as a
        # fallback; BTC's real price had since moved to ~84000, and
        # BingX's server-side validation correctly rejected the resulting
        # stale take-profit target ("should be greater than the current
        # price") - a real live failure, not a hypothetical one, and a
        # good confirmation that this bracket-failure path (entry filled,
        # a leg rejected, emergency flatten) works correctly under an
        # unplanned real condition, not just in the unit tests.
        binance_rest = BinanceFuturesRestClient(testnet=True)
        try:
            klines = await binance_rest.get_klines(_SYMBOL, "1m", limit=1)
        finally:
            await binance_rest.aclose()
        entry_price_reference = klines[-1].close

        raw_qty = (btc_rules.min_notional * 1.30) / entry_price_reference
        step = Decimal(str(btc_rules.step_size))
        quantity = float((Decimal(str(raw_qty)) / step).quantize(Decimal("1"), rounding=ROUND_DOWN) * step)
        if quantity <= 0:
            quantity = btc_rules.step_size

        balances = await rest.get_account_balance()
        vst = next((b for b in balances if b.get("asset") == "VST"), None)
        vst_balance = float(vst.get("balance", 0)) if vst else 0.0
        log_event(
            _LOG, "starting", symbol=_SYMBOL, quantity=quantity, vst_balance=vst_balance,
            reference_price=entry_price_reference,
        )

        # stop/TP deliberately far away (5%) so they don't accidentally
        # trigger during the few seconds this script needs to verify and clean up
        stop_price = round(entry_price_reference * 0.95, btc_rules.price_precision)
        take_profit_price = round(entry_price_reference * 1.05, btc_rules.price_precision)

        log_event(
            _LOG, "opening_test_bracket", symbol=_SYMBOL, side="LONG", quantity=quantity,
            stop_price=stop_price, take_profit_price=take_profit_price,
        )
        brackets = await execution.open_bracket_position(
            _SYMBOL, "LONG", quantity, stop_price, take_profit_price, _LEVERAGE,
        )
        log_event(
            _LOG, "bracket_opened",
            entry_order_id=brackets.entry.order_id, entry_status=brackets.entry.status,
            entry_avg_price=brackets.entry.avg_price, entry_executed_qty=brackets.entry.executed_qty,
            stop_order_id=brackets.stop.order_id, stop_status=brackets.stop.status,
            tp_order_id=brackets.take_profit.order_id, tp_status=brackets.take_profit.status,
        )

        log_event(_LOG, "verifying_against_exchange", message="Confirming the exchange agrees with what we just did")
        live_position = await execution.get_position(_SYMBOL)
        assert live_position.position_amt > 0, f"expected a positive open position, got {live_position.position_amt}"
        stop_status = await execution.get_order_status(_SYMBOL, brackets.stop.order_id)
        tp_status = await execution.get_order_status(_SYMBOL, brackets.take_profit.order_id)
        assert stop_status.status in ("NEW",), f"stop order unexpected status: {stop_status.status}"
        assert tp_status.status in ("NEW",), f"take-profit order unexpected status: {tp_status.status}"
        log_event(
            _LOG, "verified_ok", position_amt=live_position.position_amt, entry_price=live_position.entry_price,
            stop_order_status=stop_status.status, tp_order_status=tp_status.status,
        )

        log_event(_LOG, "cleaning_up", message="Undoing the test - cancelling both protective orders, then flattening")
        await execution.cancel_leftover_order(_SYMBOL, brackets.stop.order_id)
        await execution.cancel_leftover_order(_SYMBOL, brackets.take_profit.order_id)
        await rest.place_market_order(_SYMBOL, "SELL", live_position.position_amt, reduce_only=True)

        final_position = await execution.get_position(_SYMBOL)
        log_event(_LOG, "cleanup_done", final_position_amt=final_position.position_amt)
        assert final_position.position_amt == 0, "cleanup failed - a position is still open, check manually!"

        log_event(
            _LOG, "verification_complete", message=(
                "Real order placement, protective bracket, exchange-side verification, and cleanup all "
                "worked end to end against BingX VST. Safe to build/run the continuous BingX shadow-trading poller."
            ),
        )
    finally:
        await rest.aclose()


if __name__ == "__main__":
    asyncio.run(_main())
