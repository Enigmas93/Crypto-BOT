"""Vectorized research backtester - evaluates the production strategy rules
over years of bars in seconds, so configurations can be compared across
dozens of symbols with in-sample / out-of-sample splits.

Parity with production is enforced by tests/test_research_parity.py: every
signal here must equal `aegis.strategy.strategies.evaluate_*` on the same
bars (indicators come from the same `aegis.technical.indicators` functions,
computed once over the full series instead of once per bar).

No lookahead: features at bar i use bars <= i only; entries fill at bar i+1
open; a stop moved using bar j's extremes only applies from bar j+1; when a
bar could hit both stop and target the stop is assumed first; a gap through
the stop fills at the open.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from aegis.technical import indicators as ta


# -- features ---------------------------------------------------------------
def _swing_levels(df: pd.DataFrame, left: int = 2, right: int = 2):
    h, l = df["high"].to_numpy(), df["low"].to_numpy()
    n = len(df)
    is_sh = np.zeros(n, bool)
    is_sl = np.zeros(n, bool)
    for i in range(left, n - right):
        lh, rh = h[i - left:i].max(), h[i + 1:i + 1 + right].max()
        if h[i] >= lh and h[i] >= rh and (h[i] > lh or h[i] > rh):
            is_sh[i] = True
        ll, rl = l[i - left:i].min(), l[i + 1:i + 1 + right].min()
        if l[i] <= ll and l[i] <= rl and (l[i] < ll or l[i] < rl):
            is_sl[i] = True

    def last_two(flags, vals):
        last = np.full(n, np.nan)
        prev = np.full(n, np.nan)
        cur_last, cur_prev = np.nan, np.nan
        for i in range(n):
            j = i - right  # a swing at j is only confirmed once bar j+right exists
            if j >= 0 and flags[j]:
                cur_prev, cur_last = cur_last, vals[j]
            last[i], prev[i] = cur_last, cur_prev
        return last, prev

    return (*last_two(is_sh, h), *last_two(is_sl, l))


def features(df: pd.DataFrame) -> pd.DataFrame:
    df = df.sort_values("open_time").reset_index(drop=True)
    c = df["close"]
    f = df[["open_time", "close_time", "open", "high", "low", "close", "volume"]].copy()
    f["ema50"] = ta.ema(c, 50)
    f["ema200"] = ta.ema(c, 200)
    f["rsi"] = ta.rsi(c, 14)
    f["atr"] = ta.atr(df, 14)
    f["adx"] = ta.adx(df, 14)
    vwap = ta.daily_anchored_vwap(df)
    f["dist_vwap"] = (c - vwap) / vwap * 100
    f["volz"] = ta.volume_zscore(df["volume"], 20)
    f["don20_hi"] = df["high"].rolling(20).max().shift(1)
    f["don20_lo"] = df["low"].rolling(20).min().shift(1)
    sh, sh_prev, sl, sl_prev = _swing_levels(df)
    trend = np.full(len(df), "UNKNOWN", dtype=object)
    have = ~np.isnan(sh_prev) & ~np.isnan(sl_prev)
    trend[have] = "RANGING"
    trend[have & (sh > sh_prev) & (sl > sl_prev)] = "UPTREND"
    trend[have & (sh < sh_prev) & (sl < sl_prev)] = "DOWNTREND"
    f["trend"] = trend
    f["breakout"] = c > sh
    f["breakdown"] = c < sl
    return f


# -- signals: (direction, strength, insufficient_data) per bar ----------------
def sig_trend_pullback(f):
    req = f[["close", "ema50", "ema200", "adx", "rsi"]].notna().all(axis=1)
    trending, pull = f["adx"] >= 20, (f["rsi"] >= 40) & (f["rsi"] <= 60)
    bull = (f["close"] > f["ema50"]) & (f["ema50"] > f["ema200"])
    bear = (f["close"] < f["ema50"]) & (f["ema50"] < f["ema200"])
    d = np.where(bull & trending & pull & (f["trend"] == "UPTREND"), 1,
                 np.where(bear & trending & pull & (f["trend"] == "DOWNTREND"), -1, 0))
    return d.astype(np.int8), (f["adx"] * 2).clip(0, 100).fillna(0).to_numpy(), ~req.to_numpy()


def sig_breakout(f):
    req = f["volz"].notna()
    conf = f["volz"] >= 1.0
    d = np.where(f["breakout"] & conf, 1, np.where(f["breakdown"] & conf, -1, 0))
    return d.astype(np.int8), (50 + f["volz"] * 15).clip(0, 100).fillna(0).to_numpy(), ~req.to_numpy()


def sig_mean_reversion(f):
    req = f[["close", "rsi", "adx", "dist_vwap"]].notna().all(axis=1)
    ranging = f["adx"] <= 20
    d = np.where(ranging & (f["rsi"] >= 70) & (f["dist_vwap"] >= 2.0), -1,
                 np.where(ranging & (f["rsi"] <= 30) & (f["dist_vwap"] <= -2.0), 1, 0))
    return d.astype(np.int8), (f["dist_vwap"].abs() * 10).clip(0, 100).fillna(0).to_numpy(), ~req.to_numpy()


def sig_donchian_trend(f, adx_min: float = 25.0):
    req = f[["close", "ema200", "adx", "don20_hi", "don20_lo"]].notna().all(axis=1)
    trending = f["adx"] >= adx_min
    up = trending & (f["close"] > f["don20_hi"]) & (f["close"] > f["ema200"])
    dn = trending & (f["close"] < f["don20_lo"]) & (f["close"] < f["ema200"])
    d = np.where(up, 1, np.where(dn, -1, 0))
    return d.astype(np.int8), np.full(len(f), 100.0), ~req.to_numpy()


SIGNALS = {
    "TREND_PULLBACK": sig_trend_pullback, "BREAKOUT": sig_breakout,
    "MEAN_REVERSION": sig_mean_reversion, "DONCHIAN_TREND": sig_donchian_trend,
}


def decisions(f: pd.DataFrame, strategy_ids, threshold: float = 30.0, weights: dict | None = None) -> np.ndarray:
    """Same math as ConfluenceEngine.combine_signals, vectorized."""
    weights = weights or {}
    num = np.zeros(len(f))
    den = np.zeros(len(f))
    for sid in strategy_ids:
        d, s, ins = SIGNALS[sid](f)
        w = weights.get(sid, 1.0)
        valid = ~ins
        num += np.where(valid, d * s * w, 0.0)
        den += np.where(valid, w, 0.0)
    net = np.divide(num, den, out=np.zeros_like(num), where=den > 0)
    return np.where(net >= threshold, 1, np.where(net <= -threshold, -1, 0)).astype(np.int8)


# -- exits / simulation ---------------------------------------------------------
@dataclass(slots=True)
class ExitRule:
    stop_atr: float = 2.0
    tp_r: float | None = 2.0
    be_trigger_r: float | None = None
    be_offset_r: float = 0.1
    trail_trigger_r: float | None = None
    trail_atr: float | None = None


@dataclass(slots=True)
class SimTrade:
    i_entry: int
    i_exit: int
    side: int
    r: float
    mfe: float
    time: pd.Timestamp


def simulate(f: pd.DataFrame, decision: np.ndarray, rule: ExitRule, fee: float = 0.0005, slip: float = 0.0003,
             allowed: np.ndarray | None = None) -> list[SimTrade]:
    o, h, l, c, atr = (f[k].to_numpy() for k in ("open", "high", "low", "close", "atr"))
    times = f["open_time"].to_numpy()
    n = len(f)
    trades: list[SimTrade] = []
    i = 0
    while i < n - 1:
        side = int(decision[i])
        if side == 0 or not atr[i] > 0 or (allowed is not None and not allowed[i]):
            i += 1
            continue
        entry = o[i + 1] * (1 + slip * side)
        risk = atr[i] * rule.stop_atr
        stop = entry - side * risk
        tp = entry + side * risk * rule.tp_r if rule.tp_r else None
        best = 0.0
        exit_px, j = None, i + 1
        while j < n:
            if (side == 1 and l[j] <= stop) or (side == -1 and h[j] >= stop):
                gapped = (side == 1 and o[j] < stop) or (side == -1 and o[j] > stop)
                exit_px = (o[j] if gapped else stop) * (1 - slip * side)
                break
            if tp is not None and ((side == 1 and h[j] >= tp) or (side == -1 and l[j] <= tp)):
                exit_px = tp
                break
            best = max(best, ((h[j] - entry) if side == 1 else (entry - l[j])) / risk)
            if rule.be_trigger_r is not None and best >= rule.be_trigger_r:
                lvl = entry + side * rule.be_offset_r * risk
                stop = max(stop, lvl) if side == 1 else min(stop, lvl)
            if rule.trail_trigger_r is not None and best >= rule.trail_trigger_r:
                lvl = entry + side * best * risk - side * rule.trail_atr * atr[i]
                stop = max(stop, lvl) if side == 1 else min(stop, lvl)
            j += 1
        if exit_px is None:
            exit_px, j = c[n - 1], n - 1
        r = ((exit_px - entry) * side - (entry + exit_px) * fee) / risk
        trades.append(SimTrade(i + 1, j, side, float(r), float(best), pd.Timestamp(times[i + 1])))
        i = j
    return trades


def summarize(rs: list[float]) -> dict:
    if not rs:
        return {"trades": 0}
    r = np.array(rs)
    eq = np.concatenate([[0.0], np.cumsum(r)])
    gains, losses = r[r > 0].sum(), -r[r < 0].sum()
    return {
        "trades": int(len(r)), "win_rate": round(float((r > 0).mean() * 100), 1),
        "avg_r": round(float(r.mean()), 4), "total_r": round(float(r.sum()), 1),
        "profit_factor": round(float(gains / losses), 2) if losses > 0 else None,
        "max_drawdown_r": round(float((np.maximum.accumulate(eq) - eq).max()), 1),
    }
