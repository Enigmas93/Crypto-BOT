#!/usr/bin/env python
"""Process supervisor: launches every long-running Aegis Quant script as a
child process and restarts any that exits unexpectedly, with exponential
backoff capped at _MAX_BACKOFF_SECONDS.

Built after a transient network blip crashed Shadow Trading and Momentum
Trading while real positions were open (2026-09-23). Per-cycle resilience
was added inside those scripts for that specific case (a transient Binance
REST failure - see their own module docstrings), but nothing restarted a
script that died for any OTHER reason: an unhandled exception, an OOM kill,
a Windows update reboot. This supervisor is that missing layer - one
process to launch, one thing to watch, instead of ten independent
background shells a human has to notice and relaunch by hand.

Does not touch strategy, risk or trading logic - it only starts, stops and
restarts the existing scripts/run_*.py entry points as opaque subprocesses.
Each of those scripts already has its own hard safety gates (testnet-only,
missing-credentials checks, etc.) - the supervisor does not duplicate or
second-guess them, it just keeps whatever they decide to do running.

A child that keeps crashing immediately (e.g. missing credentials) is not
special-cased: it settles into a restart attempt every _MAX_BACKOFF_SECONDS
rather than spinning tight, and recovers on its own once the underlying
issue is fixed - no manual restart needed either way.

Known limitation: on Windows, forcibly killing this supervisor itself
(`taskkill //F`) does not run its shutdown cleanup, so children are left
running independently rather than being terminated with it. A graceful
stop (Ctrl+C in its own console, or a SIGTERM the process actually
receives) does terminate every child. This matters only for intentionally
tearing everything down at once - it does not affect the restart behavior
above.

Usage (from backend/, venv active):
    python scripts/supervisor.py

Ctrl+C stops the supervisor and terminates every child cleanly.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aegis.config import get_settings  # noqa: E402
from aegis.logging_utils import configure_logging, get_logger, log_event  # noqa: E402
from aegis.notifications.telegram import TelegramNotifier  # noqa: E402
from aegis.utils.backoff import BackoffPolicy  # noqa: E402

_LOG = get_logger("scripts.supervisor")

_SCRIPTS_DIR = Path(__file__).resolve().parent
_PYTHON = sys.executable

# Every long-running script this bot needs up at all times. Order doesn't
# matter - each runs as an independent child process.
_SUPERVISED_SCRIPTS = [
    "run_collector.py",
    "run_derivatives_engine.py",
    "run_liquidation_engine.py",
    "run_orderbook_engine.py",
    "run_macro_engine.py",
    "run_news_engine.py",
    "run_event_risk_engine.py",
    "run_paper_trading.py",
    "run_shadow_trading.py",
    "run_bingx_shadow_trading.py",
    "run_momentum_trading.py",
    "run_bingx_momentum_trading.py",
    "run_daily_reset.py",
    "run_dashboard.py",
]

_POLL_INTERVAL_SECONDS = 5.0
# A child that stayed up at least this long resets its own backoff policy -
# a script that crashes once after running fine for hours shouldn't inherit
# a long wait from an old, unrelated incident.
_BACKOFF_RESET_AFTER_SECONDS = 600.0


class _Supervised:
    def __init__(self, script: str, notifier: TelegramNotifier | None = None) -> None:
        self.script = script
        self.process: asyncio.subprocess.Process | None = None
        self.started_at: float = 0.0
        self.next_restart_at: float | None = None
        # Same policy every reconnecting client in this codebase already
        # uses (aegis.utils.backoff) - 1s base, 60s cap, 2x multiplier.
        self.backoff = BackoffPolicy()
        self.notifier = notifier

    async def start(self) -> None:
        self.process = await asyncio.create_subprocess_exec(_PYTHON, str(_SCRIPTS_DIR / self.script))
        self.started_at = asyncio.get_event_loop().time()
        self.next_restart_at = None
        log_event(_LOG, "child_started", script=self.script, pid=self.process.pid, restart_attempt=self.backoff.attempt)

    async def stop(self) -> None:
        if self.process is None or self.process.returncode is not None:
            return
        self.process.terminate()
        try:
            await asyncio.wait_for(self.process.wait(), timeout=10)
        except asyncio.TimeoutError:
            self.process.kill()
            await self.process.wait()

    async def check_and_maybe_restart(self, now: float) -> None:
        if self.process.returncode is None:
            return  # still running
        if self.next_restart_at is None:
            uptime = now - self.started_at
            if uptime >= _BACKOFF_RESET_AFTER_SECONDS:
                self.backoff.reset()
            # Checked before next_delay() advances it - True only for the
            # FIRST failure of a new streak, so a crash-loop alerts once
            # when it starts, not on every retry (which would just spam a
            # channel once every _MAX_BACKOFF_SECONDS forever).
            is_new_failure_streak = self.backoff.attempt == 0
            delay = self.backoff.next_delay()
            self.next_restart_at = now + delay
            log_event(
                _LOG, "child_exited", level=40, script=self.script, exit_code=self.process.returncode,
                uptime_seconds=round(uptime, 1), restart_in_seconds=round(delay, 1),
            )
            if is_new_failure_streak and self.notifier is not None:
                await self.notifier.send(
                    f"⚠️ Supervisor: '{self.script}' caiu (exit code {self.process.returncode}) "
                    f"depois de {round(uptime, 1)}s no ar. Reiniciando automaticamente."
                )
            return
        if now >= self.next_restart_at:
            await self.start()


async def _run_supervisor() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    notifier = TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id)
    children = [_Supervised(script, notifier=notifier) for script in _SUPERVISED_SCRIPTS]
    for child in children:
        await child.start()
    log_event(_LOG, "supervisor_started", scripts=_SUPERVISED_SCRIPTS)

    try:
        while True:
            await asyncio.sleep(_POLL_INTERVAL_SECONDS)
            now = asyncio.get_event_loop().time()
            for child in children:
                await child.check_and_maybe_restart(now)
    finally:
        log_event(_LOG, "supervisor_stopping")
        await asyncio.gather(*(child.stop() for child in children))
        await notifier.aclose()


if __name__ == "__main__":
    try:
        asyncio.run(_run_supervisor())
    except KeyboardInterrupt:
        pass
