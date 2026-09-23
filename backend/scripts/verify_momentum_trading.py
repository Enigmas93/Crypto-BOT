#!/usr/bin/env python
"""ONE-SHOT, manual live validation of the Momentum Engine's real-order
pipeline (Fase 14) - run this BEFORE ever starting the continuous poller
(run_momentum_trading.py). Two parts:

  1. Read-only: scans real 24h Binance stats and prints the current
     liquid-momentum ranking - proves the Scanner works against real data.
  2. Places one small real TRAILING_STOP_MARKET bracket on testnet,
     verifies every piece against what the exchange itself reports, then
     immediately undoes it - same pattern as verify_shadow_trading.py.

Hard safety gate: refuses to run at all unless BINANCE_TESTNET=true and
LIVE_TRADING=false.

Usage (from backend/, venv active):
    python scripts/verify_momentum_trading.py
"""
from __future__ import annotations

import asyncio
import sys
from decimal import ROUND_DOWN, Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aegis.config import get_settings  # noqa: E402
from aegis.execution.binance_provider import BinanceExecutionProvider  # noqa: E402
from aegis.logging_utils import configure_logging, get_logger, log_event  # noqa: E402
from aegis.providers.binance.rest_client import BinanceFuturesRestClient  # noqa: E402
from aegis.scanner.ranking import rank_by_momentum  # noqa: E402

_LOG = get_logger("scripts.verify_momentum_trading")
_TEST_SYMBOL = "ETHUSDT"  # liquid, already validated for order placement in Phase 10


async def _main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)

    if not settings.binance_testnet or settings.live_trading:
        log_event(
            _LOG, "refusing_to_run", level=40,
            message="This script places a real order - only ever runs against "
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
    execution = BinanceExecutionProvider(rest)

    try:
        log_event(_LOG, "step_1_scan", message="Ranking real liquid symbols by 24h momentum")
        tickers = await rest.get_24h_tickers()
        candidates = rank_by_momentum(tickers, min_quote_volume=settings.momentum_min_quote_volume, top_n=5)
        log_event(
            _LOG, "scan_result",
            candidates=[{"symbol": c.symbol, "momentum_score": round(c.momentum_score, 2),
                         "quote_volume_millions": round(c.quote_volume / 1_000_000, 1)} for c in candidates],
        )
        assert candidates, "scan produced zero candidates - min_quote_volume may be set unreasonably high"

        log_event(_LOG, "step_2_opening_trailing_bracket", symbol=_TEST_SYMBOL)
        rules = await rest.get_symbol_rules()
        btc_rules = rules[_TEST_SYMBOL]
        klines = await rest.get_klines(_TEST_SYMBOL, "1m", limit=1)
        current_price = klines[-1].close

        # 30% buffer, not 5%: floor-rounding to step_size can itself remove
        # more than a 5% margin's worth of notional (found live, on
        # ETHUSDT: step_size=0.001 at ~$2750/unit is ~$2.75 of notional per
        # step - a 5% buffer on a $20 floor was consumed entirely by a
        # single rounding step). aegis.risk.sizing.calculate_position_size
        # (production code) checks notional AFTER rounding, not before -
        # confirmed correct by reading it; this script just needs a safer
        # margin for its own manual quantity math.
        raw_qty = (btc_rules.min_notional * 1.30) / current_price
        step = Decimal(str(btc_rules.step_size))
        quantity = float((Decimal(str(raw_qty)) / step).quantize(Decimal("1"), rounding=ROUND_DOWN) * step)
        stop_price = round(current_price * 0.95, btc_rules.price_precision)

        log_event(
            _LOG, "opening", symbol=_TEST_SYMBOL, side="LONG", quantity=quantity,
            current_price=current_price, stop_price=stop_price, callback_rate_pct=2.0,
        )
        brackets = await execution.open_trailing_bracket_position(_TEST_SYMBOL, "LONG", quantity, stop_price, 2.0)
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
        await rest.place_market_order(_TEST_SYMBOL, "SELL", position.position_amt, reduce_only=True)

        final_position = await execution.get_position(_TEST_SYMBOL)
        log_event(_LOG, "cleanup_done", final_position_amt=final_position.position_amt)
        assert final_position.position_amt == 0, "cleanup failed - a position is still open, check manually!"

        log_event(
            _LOG, "verification_complete", message=(
                "Scanner ranking and TRAILING_STOP_MARKET bracket both worked end to end against "
                "testnet. Safe to build the continuous momentum poller."
            ),
        )
    finally:
        await rest.aclose()


if __name__ == "__main__":
    asyncio.run(_main())
