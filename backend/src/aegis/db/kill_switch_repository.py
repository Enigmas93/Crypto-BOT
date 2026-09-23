"""Persistence for the global Kill Switch (Phase 7b, spec section 23).

`check_and_maybe_trigger` is the detection path: called after any account
state change (a trade outcome, typically). If the account is already
triggered it's a cheap no-op read - the point of stickiness is that once
tripped, further bad outcomes don't matter and don't spam
`kill_switch_events` with repeat TRIGGERED rows. `reset` is the only way
back to untriggered, and it's deliberately not idempotent: resetting an
account that was never triggered is almost certainly a caller bug, not a
harmless no-op, so it raises instead of silently succeeding.
"""
from __future__ import annotations

from dataclasses import dataclass

import asyncpg

from aegis.risk.kill_switch import evaluate_kill_switch_triggers


@dataclass(slots=True)
class KillSwitchState:
    account_id: str
    is_triggered: bool
    reasons: list[str] | None
    triggered_at: object | None  # datetime | None - kept loose to avoid importing datetime just for the hint


class KillSwitchRepository:
    def __init__(self, pool: asyncpg.Pool, notifier=None) -> None:
        self._pool = pool
        # Optional TelegramNotifier (aegis.notifications.telegram) - a
        # trigger/reset here is exactly the kind of thing a human needs to
        # know about without having to be watching the dashboard. None is
        # the default everywhere except the real trading scripts, so
        # dashboard/tests/demos never need to know this exists.
        self._notifier = notifier

    @staticmethod
    def _to_state(row: asyncpg.Record) -> KillSwitchState:
        return KillSwitchState(
            account_id=row["account_id"], is_triggered=row["is_triggered"],
            reasons=row["reasons"], triggered_at=row["triggered_at"],
        )

    async def get_state(self, account_id: str) -> KillSwitchState:
        """Never returns None - an account with no row yet is simply not
        triggered, same as if it had an explicit untriggered row."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM kill_switch_state WHERE account_id = $1", account_id)
        if row is None:
            return KillSwitchState(account_id=account_id, is_triggered=False, reasons=None, triggered_at=None)
        return self._to_state(row)

    async def check_and_maybe_trigger(
        self, account_id: str, equity: float, peak_equity: float, consecutive_losses: int, settings,
    ) -> KillSwitchState:
        current = await self.get_state(account_id)
        if current.is_triggered:
            return current

        reasons = evaluate_kill_switch_triggers(
            equity, peak_equity, consecutive_losses,
            settings.max_drawdown, settings.loss_streak_halt_threshold,
        )
        if not reasons:
            return current

        async with self._pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    INSERT INTO kill_switch_state (account_id, is_triggered, reasons, triggered_at, updated_at)
                    VALUES ($1, true, $2, now(), now())
                    ON CONFLICT (account_id) DO UPDATE SET
                        is_triggered = true, reasons = $2, triggered_at = now(), updated_at = now()
                    RETURNING *
                    """,
                    account_id, reasons,
                )
                await conn.execute(
                    "INSERT INTO kill_switch_events (account_id, action, reasons) VALUES ($1, 'TRIGGERED', $2)",
                    account_id, reasons,
                )
        if self._notifier is not None:
            await self._notifier.send(
                f"\U0001f6d1 KILL SWITCH DISPARADO - conta '{account_id}'\nMotivo(s): {', '.join(reasons)}"
            )
        return self._to_state(row)

    async def reset(self, account_id: str, note: str) -> KillSwitchState:
        current = await self.get_state(account_id)
        if not current.is_triggered:
            raise ValueError(f"kill switch for account_id={account_id!r} is not triggered - nothing to reset")

        async with self._pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    UPDATE kill_switch_state SET
                        is_triggered = false, reasons = NULL, triggered_at = NULL, updated_at = now()
                    WHERE account_id = $1
                    RETURNING *
                    """,
                    account_id,
                )
                await conn.execute(
                    "INSERT INTO kill_switch_events (account_id, action, note) VALUES ($1, 'RESET', $2)",
                    account_id, note,
                )
        if self._notifier is not None:
            await self._notifier.send(f"✅ Kill Switch resetado - conta '{account_id}'\nNota: {note}")
        return self._to_state(row)

    async def fetch_events(self, account_id: str, limit: int = 20) -> list[dict]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM kill_switch_events WHERE account_id = $1 ORDER BY occurred_at DESC LIMIT $2",
                account_id, limit,
            )
        return [dict(r) for r in rows]
