"""Backtest of the scalping strategy with the spec's session rules and BingX costs.

Fills: entry at the NEXT 1m bar's open (never the confirmation close itself),
cancelled if it gaps more than `max_entry_slip_atr` ATR5 against us. Stop is
checked before targets inside a bar (worst case); a gap through the stop
fills at the open. Costs: taker fee + slippage on entry and stop exits,
maker fee on take-profit (resting limit order).
Session (UTC day): risk 0.5%, 0.25% after 2 consecutive losses, stop after 3
consecutive losses or -2% on the day, max 5 trades; 3-bar cooldown after an
exit.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from aegis.scalping.strategy import Costs, _trend, evaluate


@dataclass(slots=True)
class ExitMode:
    name: str
    breakeven_at_r: float | None = None     # move stop to entry(+costs) once +X R
    partial_at_tp1: float = 0.0             # fraction closed at TP1 (0 = all-or-nothing at TP1)
    trail_atr: float | None = None          # trail the remainder by X ATR5 after TP1


EXIT_MODES = {
    "tp2r": ExitMode("Alvo fixo 2R"),
    "be1r": ExitMode("Alvo 2R + breakeven em 1R", breakeven_at_r=1.0),
    "partial": ExitMode("50% em 2R + trailing 1 ATR no resto", partial_at_tp1=0.5, trail_atr=1.0),
}


@dataclass(slots=True)
class ScalpTrade:
    symbol: str
    side: str
    signal_time: pd.Timestamp
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    entry: float
    stop: float
    exit_price: float
    r: float                  # net result in R (risk = |entry - stop| + costs)
    pnl_pct: float            # % of equity (risk_pct * r)
    score: int
    zone: str
    exit_reason: str


def run_backtest(f: pd.DataFrame, symbol: str, mode: ExitMode, costs: Costs = Costs(),
                 max_entry_slip_atr: float = 0.1, session_rules: bool = True) -> list[ScalpTrade]:
    o, h, l, c = (f[k].to_numpy() for k in ("open", "high", "low", "close"))
    atr5 = f["m5_atr"].to_numpy()
    times = f["close_time"]
    days = times.dt.floor("D").to_numpy()
    # cheap pre-gate: only rows where the 15m trend is aligned can ever signal
    e9, e21, e50 = (f[k].to_numpy() for k in ("m15_ema9", "m15_ema21", "m15_ema50"))
    gate = ((e9 > e21) & (e21 > e50)) | ((e9 < e21) & (e21 < e50))

    trades: list[ScalpTrade] = []
    n = len(f)
    i = 25
    cooldown_until = -1
    day, day_pnl, day_trades, streak = None, 0.0, 0, 0
    while i < n - 2:
        if days[i] != day:
            day, day_pnl, day_trades, streak = days[i], 0.0, 0, 0
        if session_rules and (day_trades >= 5 or streak >= 3 or day_pnl <= -2.0):
            i += 1
            continue
        if i < cooldown_until or not gate[i]:
            i += 1
            continue
        d = evaluate(f, i, costs)
        if d.signal == "NO_TRADE":
            i += 1
            continue
        sign = 1 if d.signal == "LONG" else -1
        e = i + 1
        if (o[e] - d.entry) * sign > max_entry_slip_atr * atr5[i]:
            i += 1                                    # slippage above limit -> cancel
            continue
        entry = o[e] * (1 + sign * costs.entry_slippage)
        stop = d.stop
        if (entry - stop) * sign <= 0:
            i += 1
            continue
        risk_px = (entry - stop) * sign
        tp1 = entry + sign * ((d.tp1 - d.entry) * sign)   # keep the planned distance from the real fill
        risk_pct = 0.25 if (session_rules and streak >= 2) else 0.5
        remaining, realized, best = 1.0, 0.0, 0.0
        cur_stop, took_tp1 = stop, False
        j, reason = e, None
        exit_px = None
        while j < n:
            hit = (l[j] <= cur_stop) if sign == 1 else (h[j] >= cur_stop)   # stop checked first
            if hit:
                gapped = (o[j] < cur_stop) if sign == 1 else (o[j] > cur_stop)
                px = (o[j] if gapped else cur_stop) * (1 - sign * costs.stop_slippage)
                realized += remaining * ((px - entry) * sign - (entry + px) * costs.taker_fee)
                exit_px, reason, remaining = px, "STOP" if not took_tp1 else "TRAIL", 0.0
                break
            tp_hit = (h[j] >= tp1) if sign == 1 else (l[j] <= tp1)
            if not took_tp1 and tp_hit:
                frac = mode.partial_at_tp1 if mode.partial_at_tp1 > 0 else 1.0
                realized += frac * ((tp1 - entry) * sign - (entry * costs.taker_fee + tp1 * costs.maker_fee))
                remaining -= frac
                took_tp1 = True
                exit_px, reason = tp1, "TP"
                if remaining <= 1e-9:
                    break
                cur_stop = entry + sign * entry * costs.round_trip_pct()   # lock breakeven on the runner
            fav = ((h[j] - entry) if sign == 1 else (entry - l[j])) / risk_px
            best = max(best, fav)
            if mode.breakeven_at_r is not None and not took_tp1 and best >= mode.breakeven_at_r:
                be = entry + sign * entry * costs.round_trip_pct()
                cur_stop = max(cur_stop, be) if sign == 1 else min(cur_stop, be)
            if took_tp1 and mode.trail_atr:
                ext = h[j] if sign == 1 else l[j]
                lvl = ext - sign * mode.trail_atr * atr5[i]
                cur_stop = max(cur_stop, lvl) if sign == 1 else min(cur_stop, lvl)
            j += 1
        if remaining > 1e-9:                             # end of data
            px = c[n - 1]
            realized += remaining * ((px - entry) * sign - (entry + px) * costs.taker_fee)
            exit_px, reason, j = px, "END", n - 1
        # R measured against the planned loss INCLUDING costs, so -1R = a full stop-out after fees
        full_loss = risk_px + entry * costs.round_trip_pct()
        r = realized / full_loss
        pnl_pct = risk_pct * r
        trades.append(ScalpTrade(symbol, d.signal, times.iloc[i], times.iloc[e], times.iloc[j], entry, stop,
                                 exit_px, r, pnl_pct, d.score, d.zone or "", reason))
        day_pnl += pnl_pct
        day_trades += 1
        streak = streak + 1 if r < 0 else 0
        cooldown_until = j + 4                             # >= 3 full 1m bars after the exit
        i = j + 1
    return trades


def summarize(trades: list[ScalpTrade]) -> dict:
    if not trades:
        return {"trades": 0}
    r = np.array([t.r for t in trades])
    pnl = np.array([t.pnl_pct for t in trades])
    eq = np.concatenate([[0.0], np.cumsum(pnl)])
    streak = best_streak = 0
    for x in r:
        streak = streak + 1 if x < 0 else 0
        best_streak = max(best_streak, streak)
    wins, losses = r[r > 0].sum(), -r[r < 0].sum()
    return {
        "trades": int(len(r)), "win_rate": round(float((r > 0).mean() * 100), 1),
        "avg_r": round(float(r.mean()), 3), "expectancy_r": round(float(r.mean()), 3),
        "profit_factor": round(float(wins / losses), 2) if losses > 0 else None,
        "net_pct": round(float(pnl.sum()), 2), "max_drawdown_pct": round(float((np.maximum.accumulate(eq) - eq).max()), 2),
        "max_loss_streak": int(best_streak),
        "per_month": round(len(r) / max(1, (trades[-1].entry_time - trades[0].entry_time).days / 30.4), 1),
    }
