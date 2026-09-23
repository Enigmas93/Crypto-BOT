"""Persistence for the Backtest Engine (Phase 8, spec section 86): one row
per run (config snapshot + final metrics) and one row per trade it
produced. `save_run` writes both in a single transaction - a run without
its trades (or vice versa) would be a silently misleading record.
"""
from __future__ import annotations

import json

import asyncpg

from aegis.backtest.models import BacktestResult


class BacktestRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def save_run(self, result: BacktestResult) -> int:
        config = result.config
        m = result.metrics

        async with self._pool.acquire() as conn:
            async with conn.transaction():
                run_id = await conn.fetchval(
                    """
                    INSERT INTO backtest_runs (
                        symbol, interval, initial_equity, fees_pct, slippage_pct,
                        strategy_ids, strategy_weights, confluence_threshold,
                        stop_atr_multiple, take_profit_r_multiple, leverage, warmup_bars,
                        risk_account_id, data_start, data_end, total_bars,
                        total_trades, wins, losses, win_rate, loss_rate,
                        gross_profit, gross_loss, net_pnl, profit_factor,
                        average_win, average_loss, expectancy, total_fees,
                        max_drawdown_pct, recovery_factor, average_r, median_r,
                        sharpe_r, sortino_r, calmar_r, max_consecutive_losses,
                        total_return_pct, final_equity,
                        kill_switch_triggered, kill_switch_reasons, kill_switch_tripped_at
                    ) VALUES (
                        $1,$2,$3,$4,$5,$6,$7::jsonb,$8,$9,$10,$11,$12,$13,$14,$15,$16,
                        $17,$18,$19,$20,$21,$22,$23,$24,$25,$26,$27,$28,$29,
                        $30,$31,$32,$33,$34,$35,$36,$37,$38,$39,$40,$41,$42
                    )
                    RETURNING id
                    """,
                    config.symbol, config.interval, config.initial_equity, config.fees_pct,
                    config.slippage_pct, list(config.strategy_ids),
                    json.dumps(config.strategy_weights) if config.strategy_weights is not None else None,
                    config.confluence_threshold, config.stop_atr_multiple, config.take_profit_r_multiple,
                    config.leverage, config.warmup_bars, config.risk_account_id,
                    result.data_start, result.data_end, result.total_bars,
                    m.total_trades, m.wins, m.losses, m.win_rate, m.loss_rate,
                    m.gross_profit, m.gross_loss, m.net_pnl, m.profit_factor,
                    m.average_win, m.average_loss, m.expectancy, m.total_fees,
                    m.max_drawdown_pct, m.recovery_factor, m.average_r, m.median_r,
                    m.sharpe_r, m.sortino_r, m.calmar_r, m.max_consecutive_losses,
                    m.total_return_pct, m.final_equity,
                    result.kill_switch_triggered, result.kill_switch_reasons or None, result.kill_switch_tripped_at,
                )

                if result.trades:
                    await conn.executemany(
                        """
                        INSERT INTO backtest_trades (
                            run_id, symbol, side, entry_time, entry_price, exit_time, exit_price,
                            exit_reason, quantity, gross_pnl, fees_paid, net_pnl, r_multiple,
                            confluence_score, reasons
                        ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)
                        """,
                        [
                            (
                                run_id, t.symbol, t.side, t.entry_time, t.entry_price, t.exit_time,
                                t.exit_price, t.exit_reason, t.quantity, t.gross_pnl, t.fees_paid,
                                t.net_pnl, t.r_multiple, t.confluence_score, t.reasons,
                            )
                            for t in result.trades
                        ],
                    )

        return run_id

    @staticmethod
    def _to_run_dict(row: asyncpg.Record) -> dict:
        d = dict(row)
        # asyncpg returns jsonb columns as raw JSON text, not a decoded
        # object, unless a type codec is registered on the pool - none is,
        # so decode it here instead of leaking the raw string to callers.
        if d.get("strategy_weights") is not None:
            d["strategy_weights"] = json.loads(d["strategy_weights"])
        return d

    async def fetch_run(self, run_id: int) -> dict | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM backtest_runs WHERE id = $1", run_id)
        return self._to_run_dict(row) if row else None

    async def fetch_trades(self, run_id: int) -> list[dict]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM backtest_trades WHERE run_id = $1 ORDER BY entry_time", run_id,
            )
        return [dict(r) for r in rows]

    async def fetch_recent_runs(self, symbol: str | None = None, limit: int = 20) -> list[dict]:
        async with self._pool.acquire() as conn:
            if symbol is not None:
                rows = await conn.fetch(
                    "SELECT * FROM backtest_runs WHERE symbol = $1 ORDER BY created_at DESC LIMIT $2",
                    symbol, limit,
                )
            else:
                rows = await conn.fetch("SELECT * FROM backtest_runs ORDER BY created_at DESC LIMIT $1", limit)
        return [self._to_run_dict(r) for r in rows]
