"""Offline checks for the process supervisor (scripts/supervisor.py). The
supervisor's actual job - spawning and restarting real OS subprocesses - is
validated live (see backend/README.md), not here; what's worth an offline
regression test is the static config that's easy to silently break with an
unrelated rename.
"""
from __future__ import annotations

import sys
from pathlib import Path

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
