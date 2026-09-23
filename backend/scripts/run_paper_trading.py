#!/usr/bin/env python
"""Runs the Phase 9 Paper Trading Engine forever: for every configured
symbol, polls the real candle history already collected (Phase 1/2/3),
checks any open paper position for an exit, or evaluates the Strategy
Engine for a new entry - gated by the Kill Switch (Phase 7b) and the real
RiskEngine (Phase 7), exactly like a live system would be.

No real orders are ever placed and no real money is at risk - every fill
is simulated (aegis.execution.fills, the same math BacktestEngine uses)
and persisted to paper_positions/paper_trades against a real, continuously
updated account (risk_account_state, shared with the Risk Engine).

Two symbol buckets (Fase 13), same account, same engine - only the
(symbol, interval) pairs differ: core_symbols trade on 1h; speculative_
symbol_list trades on a shorter interval (SPECULATIVE_INTERVAL, 15m by
default) - a "quick return" opportunity set the 1h loop is too slow to
catch. Not a full Core/Growth/Speculative portfolio-allocation system (no
target percentages, no rebalancing) - just a second interval the same
RiskEngine-gated pipeline evaluates.

Run the collector first (Phase 1/2) so there is real, continuously
updating candle history to act on:
    python scripts/run_collector.py          # in one terminal
    python scripts/run_paper_trading.py       # in another

Usage (from backend/, venv active):
    python scripts/run_paper_trading.py

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
from aegis.db.paper_repository import PaperRepository  # noqa: E402
from aegis.db.risk_repository import RiskRepository  # noqa: E402
from aegis.logging_utils import configure_logging, get_logger, log_event  # noqa: E402
from aegis.paper.engine import PaperTradingEngine  # noqa: E402
from aegis.paper.models import PaperTradingConfig  # noqa: E402
from aegis.providers.binance.rest_client import BinanceFuturesRestClient  # noqa: E402

_LOG = get_logger("scripts.run_paper_trading")
_CORE_INTERVAL = "1h"
_STARTING_EQUITY = 1000.0


async def _main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)

    rest = BinanceFuturesRestClient(testnet=settings.binance_testnet)
    pool = await create_pool(settings)
    candle_repo = CandleRepository(pool)
    paper_repo = PaperRepository(pool)
    risk_repo = RiskRepository(pool)
    kill_switch_repo = KillSwitchRepository(pool)
    engine = PaperTradingEngine(candle_repo, paper_repo, risk_repo, kill_switch_repo, settings)

    try:
        rules_by_symbol = await rest.get_symbol_rules()
        await risk_repo.initialize_account_state(settings.paper_account_id, starting_equity=_STARTING_EQUITY)

        configs = [
            PaperTradingConfig(symbol=symbol, interval=_CORE_INTERVAL, account_id=settings.paper_account_id,
                                candle_limit=settings.paper_trading_candle_limit)
            for symbol in settings.core_symbols
        ] + [
            PaperTradingConfig(symbol=symbol, interval=settings.speculative_interval,
                                account_id=settings.paper_account_id, candle_limit=settings.paper_trading_candle_limit)
            for symbol in settings.speculative_symbol_list
        ]
        log_event(
            _LOG, "startup", account_id=settings.paper_account_id,
            core_symbols=settings.core_symbols, core_interval=_CORE_INTERVAL,
            speculative_symbols=settings.speculative_symbol_list, speculative_interval=settings.speculative_interval,
            poll_interval=settings.paper_trading_poll_interval_seconds,
        )

        while True:
            for config in configs:
                symbol_rules = rules_by_symbol.get(config.symbol)
                if symbol_rules is None:
                    log_event(_LOG, "unknown_symbol_rules", level=30, symbol=config.symbol)
                    continue
                result = await engine.run_once(config, symbol_rules)
                action = result.pop("action")
                if action == "POSITION_CLOSED":
                    trade = result.pop("trade")
                    log_event(
                        _LOG, "position_closed", symbol=config.symbol, side=trade.side,
                        exit_reason=trade.exit_reason, net_pnl=round(trade.net_pnl, 2),
                        r_multiple=round(trade.r_multiple, 3), **result,
                    )
                elif action in ("NO_NEW_CANDLE",):
                    pass  # expected every cycle between candle closes - not worth logging each time
                else:
                    log_event(_LOG, "cycle", symbol=config.symbol, action=action, **result)
            await asyncio.sleep(settings.paper_trading_poll_interval_seconds)
    finally:
        await rest.aclose()
        await close_pool(pool)


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
