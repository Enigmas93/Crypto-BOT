#!/usr/bin/env python
"""Runs the Phase 8 Backtest Engine against REAL persisted candle history.

Unlike the `run_*_engine.py` pollers, a backtest is a one-shot replay over
data that already exists - there's nothing to poll. This script:

  1. Pulls real BTCUSDT/ETHUSDT symbol rules from Binance (tick/step/
     minNotional), same as the Risk Engine demo - sizing must respect real
     exchange precision, not made-up numbers.
  2. Reads the real 1h candle history already collected in TimescaleDB
     (Phase 3's collector has been running since Phase 2).
  3. Runs BacktestEngine.run() for each symbol using all three built
     strategies in confluence, and prints the resulting metrics/trades.

This is the live/real-data validation step for Phase 8 - synthetic data in
tests/test_backtest_engine.py proves the replay loop's mechanics (no
lookahead, correct fills, correct risk gating); this script proves it
behaves sanely on real market data end to end.

Usage (from backend/, venv active):
    python scripts/run_backtest.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aegis.backtest.engine import BacktestEngine  # noqa: E402
from aegis.backtest.models import BacktestConfig  # noqa: E402
from aegis.config import get_settings  # noqa: E402
from aegis.db.backtest_repository import BacktestRepository  # noqa: E402
from aegis.db.candle_repository import CandleRepository  # noqa: E402
from aegis.db.engine import close_pool, create_pool  # noqa: E402
from aegis.logging_utils import configure_logging, get_logger, log_event  # noqa: E402
from aegis.providers.binance.rest_client import BinanceFuturesRestClient  # noqa: E402

_LOG = get_logger("scripts.run_backtest")
_SYMBOLS = ["BTCUSDT", "ETHUSDT"]
_INTERVAL = "1h"


async def _main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)

    rest = BinanceFuturesRestClient(testnet=settings.binance_testnet)
    pool = await create_pool(settings)
    candle_repo = CandleRepository(pool)
    backtest_repo = BacktestRepository(pool)
    engine = BacktestEngine(candle_repo, settings)

    try:
        rules_by_symbol = await rest.get_symbol_rules()

        for symbol in _SYMBOLS:
            symbol_rules = rules_by_symbol[symbol]
            config = BacktestConfig(symbol=symbol, interval=_INTERVAL, initial_equity=1000.0, warmup_bars=210)

            log_event(_LOG, "backtest_start", symbol=symbol, interval=_INTERVAL,
                       initial_equity=config.initial_equity, strategy_ids=list(config.strategy_ids))

            result = await engine.run(config, symbol_rules, candle_limit=500)
            m = result.metrics

            run_id = await backtest_repo.save_run(result)

            log_event(
                _LOG, "backtest_result", symbol=symbol, run_id=run_id,
                total_trades=m.total_trades, wins=m.wins, losses=m.losses,
                win_rate=round(m.win_rate, 4), profit_factor=round(m.profit_factor, 3) if m.profit_factor else None,
                net_pnl=round(m.net_pnl, 2), total_fees=round(m.total_fees, 2),
                expectancy=round(m.expectancy, 4), average_r=round(m.average_r, 3) if m.average_r is not None else None,
                max_drawdown_pct=round(m.max_drawdown_pct, 4), max_consecutive_losses=m.max_consecutive_losses,
                sharpe_r=round(m.sharpe_r, 3) if m.sharpe_r is not None else None,
                total_return_pct=round(m.total_return_pct, 3), final_equity=round(m.final_equity, 2),
                kill_switch_triggered=result.kill_switch_triggered, kill_switch_reasons=result.kill_switch_reasons,
            )

            for trade in result.trades:
                log_event(
                    _LOG, "trade", symbol=symbol, side=trade.side,
                    entry_time=trade.entry_time.isoformat(), entry_price=round(trade.entry_price, 2),
                    exit_time=trade.exit_time.isoformat(), exit_price=round(trade.exit_price, 2),
                    exit_reason=trade.exit_reason, net_pnl=round(trade.net_pnl, 2),
                    r_multiple=round(trade.r_multiple, 3), confluence_score=round(trade.confluence_score, 1),
                )
    finally:
        await rest.aclose()
        await close_pool(pool)


if __name__ == "__main__":
    asyncio.run(_main())
