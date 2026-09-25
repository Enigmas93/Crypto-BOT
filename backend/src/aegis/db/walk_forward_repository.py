"""Persistence for WalkForwardEngine runs (Fase 17). Per-fold detail is
stored as JSONB (see migration 0019's docstring for why - a research tool
read whole-run, not a table that needs row-level fold access across runs).
"""
from __future__ import annotations

import json

import asyncpg

from aegis.backtest.walk_forward import WalkForwardResult


class WalkForwardRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def save_run(self, result: WalkForwardResult, strategy_ids: tuple[str, ...]) -> int:
        folds = [
            {
                "fold_index": f.fold_index,
                "window_start": f.window_start.isoformat(),
                "window_end": f.window_end.isoformat(),
                "total_trades": f.result.metrics.total_trades,
                "win_rate": f.result.metrics.win_rate,
                "net_pnl": f.result.metrics.net_pnl,
                "max_drawdown_pct": f.result.metrics.max_drawdown_pct,
                "kill_switch_triggered": f.result.kill_switch_triggered,
            }
            for f in result.folds
        ]
        async with self._pool.acquire() as conn:
            return await conn.fetchval(
                """
                INSERT INTO walk_forward_runs (
                    symbol, interval, strategy_ids, window_bars, step_bars, fold_count,
                    profitable_fold_pct, average_net_pnl, worst_fold_net_pnl,
                    any_fold_kill_switch_triggered, folds
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11::jsonb)
                RETURNING id
                """,
                result.symbol, result.interval, list(strategy_ids), result.window_bars, result.step_bars,
                len(result.folds), result.profitable_fold_pct, result.average_net_pnl, result.worst_fold_net_pnl,
                result.any_fold_kill_switch_triggered, json.dumps(folds),
            )

    async def fetch_recent_runs(self, limit: int = 20) -> list[dict]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, symbol, interval, strategy_ids, window_bars, step_bars, fold_count,
                       profitable_fold_pct, average_net_pnl, worst_fold_net_pnl,
                       any_fold_kill_switch_triggered, created_at
                FROM walk_forward_runs ORDER BY created_at DESC LIMIT $1
                """,
                limit,
            )
        return [dict(r) for r in rows]

    async def fetch_run(self, run_id: int) -> dict | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM walk_forward_runs WHERE id = $1", run_id)
        if row is None:
            return None
        out = dict(row)
        out["folds"] = json.loads(out["folds"]) if isinstance(out["folds"], str) else out["folds"]
        return out
