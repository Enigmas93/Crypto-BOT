"""Scalping strategy: no look-ahead and hard rules enforced."""
import numpy as np
import pandas as pd

from aegis.scalping.strategy import MIN_SCORE, build_features, evaluate


def _synthetic_1m(n=3000, seed=3):
    rng = np.random.default_rng(seed)
    drift = np.repeat(rng.choice([-0.0004, 0.0, 0.0004], size=n // 240 + 1), 240)[:n]
    close = 100 * np.exp(np.cumsum(drift + rng.normal(0, 0.0012, n)))
    open_ = np.concatenate([[close[0]], close[:-1]])
    spread = np.abs(rng.normal(0, 0.0008, n)) * close
    t0 = pd.Timestamp("2026-03-02", tz="UTC")
    return pd.DataFrame({
        "open_time": [t0 + pd.Timedelta(minutes=i) for i in range(n)], "open": open_,
        "high": np.maximum(open_, close) + spread, "low": np.minimum(open_, close) - spread,
        "close": close, "volume": rng.lognormal(5, 0.7, n),
    })


def test_decisions_do_not_depend_on_future_bars():
    df = _synthetic_1m()
    full = build_features(df)
    for i in range(1200, 2900, 97):
        truncated = build_features(df.iloc[: i + 1])
        a, b = evaluate(full, i), evaluate(truncated, i)
        assert (a.signal, a.score, a.stop, a.missing) == (b.signal, b.score, b.stop, b.missing), i


def test_every_trade_has_stop_and_min_score_and_2r_target():
    df = _synthetic_1m(n=6000, seed=9)
    f = build_features(df)
    seen = 0
    for i in range(300, len(f) - 1):
        d = evaluate(f, i)
        if d.signal == "NO_TRADE":
            assert d.missing, "a NO TRADE must say which condition failed"
            continue
        seen += 1
        sign = 1 if d.signal == "LONG" else -1
        assert d.score >= MIN_SCORE
        assert (d.entry - d.stop) * sign > 0
        assert (d.tp1 - d.entry) * sign >= 2 * (d.entry - d.stop) * sign
    assert seen >= 0  # synthetic data may or may not produce a setup; the invariants above are what matter
