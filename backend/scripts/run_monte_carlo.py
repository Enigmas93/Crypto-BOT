#!/usr/bin/env python
"""Runs a real backtest, then Monte Carlo-resamples its trade sequence -
the same real candle history and BacktestEngine as scripts/run_backtest.py,
one extra step on top (see aegis/backtest/monte_carlo.py's module
docstring for why resampling matters beyond a single historical path).

Does not persist Monte Carlo results anywhere - this is a read-only
analysis tool, run on demand, same spirit as demo_risk_engine.py /
demo_kill_switch.py. The backtest run itself IS still saved via
BacktestRepository, same as run_backtest.py, so it shows up on the
dashboard's Backtests card regardless.

Usage (from backend/, venv active):
    python scripts/run_monte_carlo.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aegis.backtest.engine import BacktestEngine  # noqa: E402
from aegis.backtest.models import BacktestConfig  # noqa: E402
from aegis.backtest.monte_carlo import run_monte_carlo  # noqa: E402
from aegis.config import get_settings  # noqa: E402
from aegis.db.backtest_repository import BacktestRepository  # noqa: E402
from aegis.db.candle_repository import CandleRepository  # noqa: E402
from aegis.db.engine import close_pool, create_pool  # noqa: E402
from aegis.logging_utils import configure_logging, get_logger, log_event  # noqa: E402
from aegis.providers.binance.rest_client import BinanceFuturesRestClient  # noqa: E402

_LOG = get_logger("scripts.run_monte_carlo")
_SYMBOLS = ["BTCUSDT", "ETHUSDT"]
_INTERVAL = "1h"
_NUM_SIMULATIONS = 2000


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

            result = await engine.run(config, symbol_rules, candle_limit=500)
            run_id = await backtest_repo.save_run(result)

            log_event(
                _LOG, "backtest_result", symbol=symbol, run_id=run_id,
                total_trades=result.metrics.total_trades, net_pnl=round(result.metrics.net_pnl, 2),
                max_drawdown_pct=round(result.metrics.max_drawdown_pct, 4),
            )

            if len(result.trades) < 2:
                log_event(
                    _LOG, "monte_carlo_skipped", level=30, symbol=symbol, run_id=run_id,
                    trade_count=len(result.trades),
                    message="too few real trades from this backtest to resample meaningfully",
                )
                continue

            mc = run_monte_carlo(result.trades, initial_equity=config.initial_equity, num_simulations=_NUM_SIMULATIONS)
            log_event(
                _LOG, "monte_carlo_result", symbol=symbol, run_id=run_id,
                num_simulations=mc.num_simulations, num_trades=mc.num_trades,
                final_equity_p5=round(mc.final_equity_p5, 2), final_equity_p50=round(mc.final_equity_p50, 2),
                final_equity_p95=round(mc.final_equity_p95, 2),
                max_drawdown_pct_p50=round(mc.max_drawdown_pct_p50, 4),
                max_drawdown_pct_p95=round(mc.max_drawdown_pct_p95, 4),
                max_drawdown_pct_worst=round(mc.max_drawdown_pct_worst, 4),
                probability_of_ruin=mc.probability_of_ruin,
            )
    finally:
        await rest.aclose()
        await close_pool(pool)


if __name__ == "__main__":
    asyncio.run(_main())
