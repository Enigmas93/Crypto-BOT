#!/usr/bin/env python
"""ONE-SHOT, manual live validation of real order placement (Phase 10) -
run this BEFORE ever starting the continuous poller (run_shadow_trading.py,
not yet built). Places one small real bracket position on Binance Futures
TESTNET, verifies every piece of it against what the exchange itself
reports, then immediately undoes it (cancels the protective orders, closes
the position) rather than leaving anything open unattended.

Hard safety gate: refuses to run at all unless BINANCE_TESTNET=true and
LIVE_TRADING=false - this script must never place a real-money order, on
principle, regardless of what the rest of the config says.

Usage (from backend/, venv active):
    python scripts/verify_shadow_trading.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aegis.config import get_settings  # noqa: E402
from aegis.execution.binance_provider import BinanceExecutionProvider  # noqa: E402
from aegis.logging_utils import configure_logging, get_logger, log_event  # noqa: E402
from aegis.providers.binance.rest_client import BinanceFuturesRestClient  # noqa: E402

_LOG = get_logger("scripts.verify_shadow_trading")
_SYMBOL = "BTCUSDT"


async def _main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)

    if not settings.binance_testnet or settings.live_trading:
        log_event(
            _LOG, "refusing_to_run", level=40,
            message="This script only ever runs against BINANCE_TESTNET=true and LIVE_TRADING=false - "
                    "it places a real order and must never do so against a real-money account.",
            binance_testnet=settings.binance_testnet, live_trading=settings.live_trading,
        )
        return
    if not settings.binance_api_key or not settings.binance_api_secret:
        log_event(_LOG, "missing_credentials", level=40, message="Run scripts/verify_execution_setup.py first")
        return

    rest = BinanceFuturesRestClient(
        testnet=True, api_key=settings.binance_api_key, api_secret=settings.binance_api_secret,
    )
    execution = BinanceExecutionProvider(rest)

    try:
        rules = await rest.get_symbol_rules()
        btc_rules = rules[_SYMBOL]
        klines = await rest.get_klines(_SYMBOL, "1m", limit=1)
        current_price = klines[-1].close

        # size the test position just over min_notional, rounded to the
        # exchange's real step size - never a made-up quantity. 30% buffer,
        # not 5%: floor-rounding to step_size can itself remove more than a
        # 5% margin's worth of notional on a symbol with a coarse step
        # relative to its price (found live on ETHUSDT while validating
        # Fase 14 - a 5% buffer was consumed entirely by a single rounding
        # step). Production sizing (aegis.risk.sizing.calculate_position_size)
        # checks notional AFTER rounding and is unaffected - this is only
        # this script's own manual quantity math.
        from decimal import Decimal, ROUND_DOWN
        raw_qty = (btc_rules.min_notional * 1.30) / current_price
        step = Decimal(str(btc_rules.step_size))
        quantity = float((Decimal(str(raw_qty)) / step).quantize(Decimal("1"), rounding=ROUND_DOWN) * step)

        # stop/TP deliberately far away (5%) so they don't accidentally
        # trigger during the few seconds this script needs to verify and clean up
        stop_price = round(current_price * 0.95, btc_rules.price_precision)
        take_profit_price = round(current_price * 1.05, btc_rules.price_precision)

        log_event(
            _LOG, "opening_test_bracket", symbol=_SYMBOL, side="LONG", quantity=quantity,
            current_price=current_price, stop_price=stop_price, take_profit_price=take_profit_price,
        )
        brackets = await execution.open_bracket_position(_SYMBOL, "LONG", quantity, stop_price, take_profit_price)
        log_event(
            _LOG, "bracket_opened",
            entry_order_id=brackets.entry.order_id, entry_status=brackets.entry.status,
            entry_avg_price=brackets.entry.avg_price, entry_executed_qty=brackets.entry.executed_qty,
            stop_order_id=brackets.stop.order_id, stop_status=brackets.stop.status,
            tp_order_id=brackets.take_profit.order_id, tp_status=brackets.take_profit.status,
        )

        log_event(_LOG, "verifying_against_exchange", message="Confirming the exchange agrees with what we just did")
        position = await execution.get_position(_SYMBOL)
        assert position.position_amt > 0, f"expected a positive open position, got {position.position_amt}"
        stop_status = await execution.get_order_status(_SYMBOL, brackets.stop.order_id)
        tp_status = await execution.get_order_status(_SYMBOL, brackets.take_profit.order_id)
        assert stop_status.status in ("NEW",), f"stop order unexpected status: {stop_status.status}"
        assert tp_status.status in ("NEW",), f"take-profit order unexpected status: {tp_status.status}"
        log_event(
            _LOG, "verified_ok", position_amt=position.position_amt, entry_price=position.entry_price,
            stop_order_status=stop_status.status, tp_order_status=tp_status.status,
        )

        log_event(_LOG, "cleaning_up", message="Undoing the test - cancelling both protective orders, then flattening")
        await execution.cancel_leftover_order(_SYMBOL, brackets.stop.order_id)
        await execution.cancel_leftover_order(_SYMBOL, brackets.take_profit.order_id)
        await rest.place_market_order(_SYMBOL, "SELL", position.position_amt, reduce_only=True)

        final_position = await execution.get_position(_SYMBOL)
        log_event(_LOG, "cleanup_done", final_position_amt=final_position.position_amt)
        assert final_position.position_amt == 0, "cleanup failed - a position is still open, check manually!"

        log_event(
            _LOG, "verification_complete", message=(
                "Real order placement, protective bracket, exchange-side verification, and cleanup all "
                "worked end to end against testnet. Safe to build the continuous shadow-trading poller."
            ),
        )
    finally:
        await rest.aclose()


if __name__ == "__main__":
    asyncio.run(_main())
