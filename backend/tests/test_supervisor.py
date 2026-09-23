"""Offline checks for the process supervisor (scripts/supervisor.py). The
supervisor's actual job - spawning and restarting real OS subprocesses - is
validated live (see backend/README.md), not here; what's worth an offline
regression test is the static config that's easy to silently break with an
unrelated rename.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import supervisor  # noqa: E402


def test_every_supervised_script_exists_on_disk():
    scripts_dir = Path(__file__).resolve().parents[1] / "scripts"
    for script in supervisor._SUPERVISED_SCRIPTS:
        assert (scripts_dir / script).is_file(), f"{script} is supervised but does not exist in scripts/"


def test_supervised_scripts_has_no_duplicates():
    assert len(supervisor._SUPERVISED_SCRIPTS) == len(set(supervisor._SUPERVISED_SCRIPTS))


def test_dashboard_and_every_trading_engine_are_supervised():
    # The scripts that place real orders or serve the UI a human relies on
    # to see them - if any of these is ever dropped from the list by
    # accident, that's a silent, serious regression.
    critical = {"run_shadow_trading.py", "run_momentum_trading.py", "run_paper_trading.py", "run_dashboard.py"}
    assert critical.issubset(set(supervisor._SUPERVISED_SCRIPTS))


class _FakeProcess:
    def __init__(self, returncode):
        self.returncode = returncode


class _FakeNotifier:
    def __init__(self):
        self.sent: list[str] = []

    async def send(self, message: str) -> None:
        self.sent.append(message)


@pytest.mark.asyncio
async def test_check_and_maybe_restart_alerts_once_on_a_new_failure_streak():
    notifier = _FakeNotifier()
    child = supervisor._Supervised("run_collector.py", notifier=notifier)
    child.process = _FakeProcess(returncode=1)
    child.started_at = 0.0

    await child.check_and_maybe_restart(now=100.0)  # first failure of the streak
    assert len(notifier.sent) == 1
    assert "run_collector.py" in notifier.sent[0]

    # Still the same failure (process hasn't been restarted yet, so
    # check_and_maybe_restart is called again before next_restart_at) -
    # must not send a second alert for the same streak.
    await child.check_and_maybe_restart(now=101.0)
    assert len(notifier.sent) == 1


@pytest.mark.asyncio
async def test_check_and_maybe_restart_does_not_alert_without_a_notifier():
    child = supervisor._Supervised("run_collector.py")  # no notifier - must not raise
    child.process = _FakeProcess(returncode=1)
    child.started_at = 0.0
    await child.check_and_maybe_restart(now=100.0)
