"""The research backtester must reproduce production strategy decisions
exactly - otherwise its evidence says nothing about what actually trades."""
import numpy as np
import pandas as pd

from aegis.research.vectorized import SIGNALS, ExitRule, decisions, features, simulate, summarize
from aegis.strategy.confluence import combine_signals
from aegis.strategy.strategies import (STRATEGY_BREAKOUT, STRATEGY_DONCHIAN_TREND, STRATEGY_MEAN_REVERSION,
                                       STRATEGY_TREND_PULLBACK, STRATEGY_FUNCTIONS)
from aegis.technical.service import compute_snapshot

DIR = {"LONG": 1, "SHORT": -1, "NO_TRADE": 0}
IDS = (STRATEGY_TREND_PULLBACK, STRATEGY_BREAKOUT, STRATEGY_MEAN_REVERSION, STRATEGY_DONCHIAN_TREND)


def _synthetic(n=620, seed=7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    drift = np.repeat(rng.choice([-0.004, 0.0, 0.004], size=n // 40 + 1), 40)[:n]
    close = 100 * np.exp(np.cumsum(drift + rng.normal(0, 0.008, n)))
    open_ = np.concatenate([[close[0]], close[:-1]])
    spread = np.abs(rng.normal(0, 0.006, n)) * close
    t0 = pd.Timestamp("2026-01-01", tz="UTC")
    return pd.DataFrame({
        "open_time": [t0 + pd.Timedelta(hours=i) for i in range(n)],
        "close_time": [t0 + pd.Timedelta(hours=i + 1) - pd.Timedelta(milliseconds=1) for i in range(n)],
        "open": open_, "high": np.maximum(open_, close) + spread, "low": np.minimum(open_, close) - spread,
        "close": close, "volume": rng.lognormal(10, 0.6, n),
    })


def test_vectorized_signals_match_production_bar_by_bar():
    df = _synthetic()
    f = features(df)
    vec = {sid: SIGNALS[sid](f) for sid in IDS}
    fired = {sid: 0 for sid in IDS}
    for i in range(230, len(df), 3):
        snap = compute_snapshot("X", "1h", df.iloc[: i + 1])
        for sid in IDS:
            p = STRATEGY_FUNCTIONS[sid](snap)
            d, s, ins = vec[sid]
            assert DIR[p.signal] == d[i], (sid, i, p.reasons)
            assert p.insufficient_data == bool(ins[i]), (sid, i)
            if p.signal != "NO_TRADE":
                fired[sid] += 1
                assert abs(p.strength - s[i]) < 1e-6, (sid, i)
    assert fired[STRATEGY_BREAKOUT] > 0 and fired[STRATEGY_DONCHIAN_TREND] > 0


def test_vectorized_confluence_matches_combine_signals():
    df = _synthetic(seed=11)
    f = features(df)
    dec = decisions(f, IDS)
    for i in range(230, len(df), 5):
        snap = compute_snapshot("X", "1h", df.iloc[: i + 1])
        prod = combine_signals([STRATEGY_FUNCTIONS[sid](snap) for sid in IDS])
        assert DIR[prod.decision] == dec[i], i


def test_simulator_stop_first_gap_fill_and_costs():
    t0 = pd.Timestamp("2026-01-01", tz="UTC")
    f = pd.DataFrame({
        "open_time": [t0 + pd.Timedelta(hours=i) for i in range(4)],
        "open": [100.0, 100.0, 95.0, 95.0], "high": [101.0, 100.5, 96.0, 96.0],
        "low": [99.0, 99.5, 90.0, 94.0], "close": [100.0, 100.0, 95.0, 95.0], "atr": [1.0] * 4,
    })
    decision = np.array([1, 0, 0, 0], dtype=np.int8)
    [t] = simulate(f, decision, ExitRule(stop_atr=2.0, tp_r=2.0), fee=0.0, slip=0.0)
    # entry 100 at bar1 open, stop 98; bar2 gaps to 95 -> filled at the open, not at 98
    assert t.i_exit == 2 and round(t.r, 6) == -2.5
    assert summarize([t.r])["trades"] == 1
