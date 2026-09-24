#!/usr/bin/env python
"""ONE-SHOT, manual live validation of the BingX Momentum Engine's real-
order pipeline (Fase 16) - mirrors scripts/verify_momentum_trading.py. Run
this AFTER scripts/verify_bingx_execution_setup.py confirms clean, and
BEFORE ever starting the continuous poller (run_bingx_momentum_trading.py).

Two parts:
  1. Read-only: scans real 24h Binance stats (same scanner every Momentum
     account uses - the ranking doesn't change depending on which exchange
     later executes) and prints the current liquid-momentum ranking.
  2. Places one small real TRAILING_STOP_MARKET bracket on BingX VST,
     verifies every piece against what the exchange itself reports, then
     immediately undoes it - same pattern as verify_bingx_shadow_trading.py.

Hard safety gate: refuses to run at all unless BINGX_TESTNET=true.

Usage (from backend/, venv active):
    python scripts/verify_bingx_momentum_trading.py
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
from aegis.scanner.ranking import rank_by_momentum  # noqa: E402

_LOG = get_logger("scripts.verify_bingx_momentum_trading")
_TEST_SYMBOL = "BTCUSDT"  # confirmed listed on BingX and already validated for order placement (Fase 16)
_LEVERAGE = 3


async def _main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)

    if not settings.bingx_testnet:
        log_event(
            _LOG, "refusing_to_run", level=40,
            message="This script places a real order - only ever runs against BINGX_TESTNET=true.",
        )
        return
    if not settings.bingx_api_key or not settings.bingx_api_secret:
        log_event(_LOG, "missing_credentials", level=40, message="Run scripts/verify_bingx_execution_setup.py first")
        return

    binance_rest = BinanceFuturesRestClient(testnet=True)
    bingx_rest = BingXFuturesRestClient(
        testnet=True, api_key=settings.bingx_api_key, api_secret=settings.bingx_api_secret,
    )
    execution = BingXExecutionProvider(bingx_rest)

    try:
        if await bingx_rest.get_position_mode():
            log_event(
                _LOG, "refusing_to_run", level=40,
                message="Account is in Hedge mode - run scripts/verify_bingx_execution_setup.py first "
                        "to switch it to one-way mode.",
            )
            return

        log_event(_LOG, "step_1_scan", message="Ranking real liquid symbols by 24h momentum (Binance data)")
        tickers = await binance_rest.get_24h_tickers()
        candidates = rank_by_momentum(tickers, min_quote_volume=settings.momentum_min_quote_volume, top_n=5)
        log_event(
            _LOG, "scan_result",
            candidates=[{"symbol": c.symbol, "momentum_score": round(c.momentum_score, 2),
                         "quote_volume_millions": round(c.quote_volume / 1_000_000, 1)} for c in candidates],
        )
        assert candidates, "scan produced zero candidates - min_quote_volume may be set unreasonably high"

        log_event(_LOG, "step_2_opening_trailing_bracket", symbol=_TEST_SYMBOL)
        rules = await bingx_rest.get_symbol_rules()
        symbol_rules = rules[_TEST_SYMBOL]
        klines = await binance_rest.get_klines(_TEST_SYMBOL, "1m", limit=1)
        current_price = klines[-1].close

        raw_qty = (symbol_rules.min_notional * 1.30) / current_price
        step = Decimal(str(symbol_rules.step_size))
        quantity = float((Decimal(str(raw_qty)) / step).quantize(Decimal("1"), rounding=ROUND_DOWN) * step)
        if quantity <= 0:
            quantity = symbol_rules.step_size
        stop_price = round(current_price * 0.95, symbol_rules.price_precision)

        log_event(
            _LOG, "opening", symbol=_TEST_SYMBOL, side="LONG", quantity=quantity,
            current_price=current_price, stop_price=stop_price, callback_rate_pct=2.0,
        )
        brackets = await execution.open_trailing_bracket_position(
            _TEST_SYMBOL, "LONG", quantity, stop_price, 2.0, _LEVERAGE,
        )
        log_event(
            _LOG, "bracket_opened",
            entry_order_id=brackets.entry.order_id, entry_status=brackets.entry.status,
            stop_order_id=brackets.stop.order_id, stop_status=brackets.stop.status,
            trailing_order_id=brackets.trailing_stop.order_id, trailing_status=brackets.trailing_stop.status,
        )

        log_event(_LOG, "step_3_verifying_against_exchange")
        position = await execution.get_position(_TEST_SYMBOL)
        assert position.position_amt > 0, f"expected a positive open position, got {position.position_amt}"
        stop_status = await execution.get_order_status(_TEST_SYMBOL, brackets.stop.order_id)
        trailing_status = await execution.get_order_status(_TEST_SYMBOL, brackets.trailing_stop.order_id)
        assert stop_status.status == "NEW", f"stop order unexpected status: {stop_status.status}"
        assert trailing_status.status == "NEW", f"trailing stop unexpected status: {trailing_status.status}"
        assert trailing_status.type == "TRAILING_STOP_MARKET"
        log_event(
            _LOG, "verified_ok", position_amt=position.position_amt,
            stop_status=stop_status.status, trailing_status=trailing_status.status,
        )

        log_event(_LOG, "step_4_cleaning_up")
        await execution.cancel_leftover_order(_TEST_SYMBOL, brackets.stop.order_id)
        await execution.cancel_leftover_order(_TEST_SYMBOL, brackets.trailing_stop.order_id)
        await bingx_rest.place_market_order(_TEST_SYMBOL, "SELL", position.position_amt, reduce_only=True)

        final_position = await execution.get_position(_TEST_SYMBOL)
        log_event(_LOG, "cleanup_done", final_position_amt=final_position.position_amt)
        assert final_position.position_amt == 0, "cleanup failed - a position is still open, check manually!"

        log_event(
            _LOG, "verification_complete", message=(
                "Scanner ranking and TRAILING_STOP_MARKET bracket both worked end to end against "
                "BingX VST. Safe to build/run the continuous BingX momentum poller."
            ),
        )
    finally:
        await binance_rest.aclose()
        await bingx_rest.aclose()


if __name__ == "__main__":
    asyncio.run(_main())
