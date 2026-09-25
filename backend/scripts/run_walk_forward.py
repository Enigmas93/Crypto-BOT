#!/usr/bin/env python
"""Runs WalkForwardEngine (Fase 17) against REAL persisted candle history -
the blueprint's missing validation step before LIVE is ever allowed for a
strategy ("passou por backtest aprovado, WALK-FORWARD aprovado..."), same
one-shot-replay-over-existing-data shape as scripts/run_backtest.py.

Splits the available history into sequential folds and runs the exact same
BacktestConfig against each one - see aegis.backtest.walk_forward's module
docstring for why this checks CONSISTENCY across periods rather than
re-optimizing parameters per fold (no parameter-fitting infrastructure
exists yet anywhere in this codebase).

Usage (from backend/, venv active):
    python scripts/run_walk_forward.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aegis.backtest.models import BacktestConfig  # noqa: E402
from aegis.backtest.walk_forward import WalkForwardEngine  # noqa: E402
from aegis.config import get_settings  # noqa: E402
from aegis.db.candle_repository import CandleRepository  # noqa: E402
from aegis.db.engine import close_pool, create_pool  # noqa: E402
from aegis.db.walk_forward_repository import WalkForwardRepository  # noqa: E402
from aegis.logging_utils import configure_logging, get_logger, log_event  # noqa: E402
from aegis.providers.binance.rest_client import BinanceFuturesRestClient  # noqa: E402

_LOG = get_logger("scripts.run_walk_forward")
_SYMBOLS = ["BTCUSDT", "ETHUSDT"]
_INTERVAL = "1h"
_WINDOW_BARS = 280   # warmup_bars=210 + ~70 bars left to actually trade on - see note below
_STEP_BARS = 140     # 50% overlap between consecutive folds
_TOTAL_BARS = 2000   # harmless if the collector has fewer than this - fetch just returns what exists
# _WINDOW_BARS is intentionally tight, not the wide multi-month window a
# mature deployment would use: checked live 2026-09-25, this project's
# collector has only ~579 1h BTCUSDT/ETHUSDT candles so far (~24 days) - a
# wider window (e.g. 500) leaves no room to roll forward more than once,
# defeating the entire point of walk-forward (checking CONSISTENCY across
# multiple periods). Revisit upward as more history accumulates; the
# WalkForwardEngine itself has no opinion on window size, this is purely a
# "how much history actually exists right now" calibration.


async def _main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)

    rest = BinanceFuturesRestClient(testnet=settings.binance_testnet)
    pool = await create_pool(settings)
    candle_repo = CandleRepository(pool)
    walk_forward_repo = WalkForwardRepository(pool)
    engine = WalkForwardEngine(candle_repo, settings)

    try:
        rules_by_symbol = await rest.get_symbol_rules()

        for symbol in _SYMBOLS:
            symbol_rules = rules_by_symbol[symbol]
            config = BacktestConfig(symbol=symbol, interval=_INTERVAL, initial_equity=1000.0, warmup_bars=210)

            log_event(_LOG, "walk_forward_start", symbol=symbol, interval=_INTERVAL,
                       window_bars=_WINDOW_BARS, step_bars=_STEP_BARS, strategy_ids=list(config.strategy_ids))

            try:
                result = await engine.run(
                    config, symbol_rules, window_bars=_WINDOW_BARS, step_bars=_STEP_BARS, total_bars=_TOTAL_BARS,
                )
            except ValueError as exc:
                log_event(_LOG, "walk_forward_skipped", level=30, symbol=symbol, reason=str(exc))
                continue

            run_id = await walk_forward_repo.save_run(result, config.strategy_ids)

            log_event(
                _LOG, "walk_forward_result", symbol=symbol, run_id=run_id, fold_count=len(result.folds),
                profitable_fold_pct=round(result.profitable_fold_pct, 4),
                average_net_pnl=round(result.average_net_pnl, 2), worst_fold_net_pnl=round(result.worst_fold_net_pnl, 2),
                any_fold_kill_switch_triggered=result.any_fold_kill_switch_triggered,
                fold_net_pnls=[round(p, 2) for p in result.fold_net_pnls],
            )
    finally:
        await rest.aclose()
        await close_pool(pool)


if __name__ == "__main__":
    asyncio.run(_main())
