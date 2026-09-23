#!/usr/bin/env python
"""Runs the Phase 11 Dashboard - a local-only web UI over the paper/shadow
trading state, risk state, backtests, and data health already built in
Phases 1-10.

Binds to localhost only by default - this API has no authentication (see
aegis/api/app.py's module docstring for why that's an acceptable trade-off
for a single-operator local tool, and why it must never be exposed beyond
that).

Usage (from backend/, venv active):
    python scripts/run_dashboard.py

Then open http://localhost:8000 in a browser. Ctrl+C to stop.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import uvicorn  # noqa: E402

if __name__ == "__main__":
    uvicorn.run("aegis.api.app:app", host="127.0.0.1", port=8000, log_level="info")
