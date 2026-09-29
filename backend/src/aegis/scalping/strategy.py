"""BingX scalping strategy (Fase 23): 15m trend -> 5m pullback -> 1m confirmation.

One implementation shared by backtest, paper and live, so what was validated
is exactly what trades. The user's written spec is translated into explicit,
testable rules below; every place where the spec was qualitative ("região
relevante", "candle de força", "rejeição") has a documented threshold.

Decision at the CLOSE of a 1m bar, using only bars closed at that instant:
1. 15m trend (mandatory): EMA9 > EMA21 > EMA50 and swing structure UPTREND
   (two ascending swing highs and lows) - mirrored for SHORT. Lateral or
   mixed -> NO TRADE.
2. 5m pullback (mandatory): within the last 6 closed 5m bars the price
   retraced >= 0.6 ATR5 from the last hour's extreme, the pullback extreme
   reached a relevant zone (5m EMA21, 5m EMA50, session VWAP, last confirmed
   5m swing low/high, previous 5m swing high/low = old breakout level) within
   0.25 ATR5 without breaking it by more than 0.5 ATR5, and the 5m close is
   still on the trend side of the 5m EMA50 (minus 0.5 ATR5).
3. 1m confirmation (all mandatory): one of the last 3 1m bars touched the zone
   (within 0.25 ATR5) and this bar closed back beyond it (rejection); this
   bar is a strength candle (body >= 60% of range, closing in the top/bottom
   30%) OR closes beyond the micro high/low of the previous 5 bars; volume
   >= 1.2x the average of the previous 20 1m bars.
4. Levels: entry reference = confirmation close; stop = pullback extreme -/+
   0.1 ATR5; rejected if the stop is < 0.35 ATR5 (noise) or > 1.5 ATR5 (too
   far), or if the entry is already > 0.5 ATR5 away from the zone. TP1 = 2R
   net of estimated round-trip costs, TP2 = 3R.
5. Safety: skip abnormal candles (range > 3x 1m ATR14) and extreme volatility
   (5m ATR in the top 3% of the last ~5 days).
6. Score 0-100 (+20 trend, +15 1m EMA9/21 aligned, +15 price vs VWAP, +15
   pullback zone, +15 1m structure, +10 volume, +10 R:R); execute only >= 75.
   Trend, zone, structure, volume and R:R are hard requirements, so a trade
   needs at least one of the two soft points (EMA9/21 or VWAP) to reach 75.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from aegis.research.vectorized import _swing_levels
from aegis.technical import indicators as ta

ZONE_TOL = 0.25          # ATR5 multiples
ZONE_BREAK = 0.5
MIN_RETRACE = 0.6
PULLBACK_BARS_5M = 6
LEG_BARS_5M = 12
REJECT_BARS_1M = 3
MICRO_BARS_1M = 5
VOL_MULT = 1.2
STOP_BUFFER = 0.1
MIN_STOP_ATR = 0.35
MAX_STOP_ATR = 1.5
MAX_CHASE_ATR = 0.5
ABNORMAL_RANGE_ATR1 = 3.0
EXTREME_VOL_PCT = 0.97
MIN_SCORE = 75


@dataclass(slots=True)
class Costs:
    taker_fee: float = 0.0005
    maker_fee: float = 0.0002
    entry_slippage: float = 0.0001
    stop_slippage: float = 0.0002

    def round_trip_pct(self) -> float:
        """Worst realistic cost of a trade as a fraction of price (entry taker + stop taker)."""
        return 2 * self.taker_fee + self.entry_slippage + self.stop_slippage


@dataclass(slots=True)
class Decision:
    signal: str                      # LONG | SHORT | NO_TRADE
    score: int = 0
    entry: float | None = None
    stop: float | None = None
    tp1: float | None = None
    tp2: float | None = None
    zone: str | None = None
    reasons: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    snapshot: dict = field(default_factory=dict)


# -- features ------------------------------------------------------------------
def _resample(df1: pd.DataFrame, rule: str, minutes: int) -> pd.DataFrame:
    g = df1.set_index("open_time").resample(rule, label="left", closed="left")
    out = pd.DataFrame({"open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(),
                        "close": g["close"].last(), "volume": g["volume"].sum(),
                        "n": g["close"].count()}).dropna()
    out = out[out["n"] == minutes].drop(columns="n").reset_index()  # complete bars only
    out["close_time"] = out["open_time"] + pd.Timedelta(minutes=minutes) - pd.Timedelta(milliseconds=1)
    return out


def _tf_features(df: pd.DataFrame, prefix: str, structure: bool) -> pd.DataFrame:
    c = df["close"]
    f = pd.DataFrame({"close_time": df["close_time"]})
    f[f"{prefix}close"] = c
    f[f"{prefix}ema9"] = ta.ema(c, 9)
    f[f"{prefix}ema21"] = ta.ema(c, 21)
    f[f"{prefix}ema50"] = ta.ema(c, 50)
    f[f"{prefix}atr"] = ta.atr(df, 14)
    if structure:
        sh, sh_prev, sl, sl_prev = _swing_levels(df)
        f[f"{prefix}sh"], f[f"{prefix}sh_prev"], f[f"{prefix}sl"], f[f"{prefix}sl_prev"] = sh, sh_prev, sl, sl_prev
    return f


def build_features(df1: pd.DataFrame, base_minutes: int = 1, mid_minutes: int = 5, high_minutes: int = 15) -> pd.DataFrame:
    """One row per base-timeframe bar with the mid/high context that was CLOSED
    at that bar's close. Defaults are the spec's 1m/5m/15m; the `m5_`/`m15_`
    column prefixes mean "mid"/"high" timeframe whatever the scale. `df1`
    must be 1m bars; they are resampled to the base timeframe first."""
    df1 = df1.sort_values("open_time").reset_index(drop=True).copy()
    if base_minutes != 1:
        df1 = _resample(df1, f"{base_minutes}min", base_minutes).drop(columns="close_time")
        df1 = df1.reset_index(drop=True)
    df1["close_time"] = df1["open_time"] + pd.Timedelta(minutes=base_minutes) - pd.Timedelta(milliseconds=1)
    f = df1[["open_time", "close_time", "open", "high", "low", "close", "volume"]].copy()
    f["ema9"] = ta.ema(df1["close"], 9)
    f["ema21"] = ta.ema(df1["close"], 21)
    f["atr1"] = ta.atr(df1, 14)
    f["vol_avg20"] = df1["volume"].rolling(20).mean().shift(1)     # previous 20 bars
    f["vwap"] = ta.daily_anchored_vwap(df1)
    df5 = _resample(df1, f"{mid_minutes}min", mid_minutes // base_minutes)
    df15 = _resample(df1, f"{high_minutes}min", high_minutes // base_minutes)
    df5["close_time"] = df5["open_time"] + pd.Timedelta(minutes=mid_minutes) - pd.Timedelta(milliseconds=1)
    df15["close_time"] = df15["open_time"] + pd.Timedelta(minutes=high_minutes) - pd.Timedelta(milliseconds=1)
    f5 = _tf_features(df5, "m5_", structure=True)
    f5["m5_leg_hi"] = df5["high"].rolling(LEG_BARS_5M).max()
    f5["m5_leg_lo"] = df5["low"].rolling(LEG_BARS_5M).min()
    f5["m5_pb_lo"] = df5["low"].rolling(PULLBACK_BARS_5M).min()
    f5["m5_pb_hi"] = df5["high"].rolling(PULLBACK_BARS_5M).max()
    f5["m5_atr_pct_rank"] = (f5["m5_atr"] / df5["close"]).rolling(1440, min_periods=288).rank(pct=True)
    f15 = _tf_features(df15, "m15_", structure=True)
    out = pd.merge_asof(f, f5, on="close_time", direction="backward")
    out = pd.merge_asof(out, f15, on="close_time", direction="backward")
    return out


# -- decision --------------------------------------------------------------------
def _trend(row) -> tuple[str | None, bool]:
    e9, e21, e50 = row["m15_ema9"], row["m15_ema21"], row["m15_ema50"]
    sh, shp, sl, slp = row["m15_sh"], row["m15_sh_prev"], row["m15_sl"], row["m15_sl_prev"]
    if any(pd.isna(v) for v in (e9, e21, e50, sh, shp, sl, slp)):
        return None, False
    if e9 > e21 > e50 and sh > shp and sl > slp:
        return "LONG", row["m15_close"] > e50
    if e9 < e21 < e50 and sh < shp and sl < slp:
        return "SHORT", row["m15_close"] < e50
    return None, False


def evaluate(f: pd.DataFrame, i: int, costs: Costs = Costs()) -> Decision:
    """Decision at the close of 1m bar `i` (uses rows <= i only)."""
    row = f.iloc[i]
    missing: list[str] = []
    if i < 25 or any(pd.isna(row[k]) for k in ("atr1", "vol_avg20", "vwap", "m5_atr", "m15_ema50")):
        return Decision("NO_TRADE", missing=["dados insuficientes"])

    side, clear_trend = _trend(row)
    if side is None:
        return Decision("NO_TRADE", missing=["tendência 15m não alinhada (EMAs/estrutura lateral ou mista)"])
    sign = 1 if side == "LONG" else -1
    atr5 = row["m5_atr"]

    # safety filters
    if row["high"] - row["low"] > ABNORMAL_RANGE_ATR1 * row["atr1"]:
        return Decision("NO_TRADE", missing=["candle anormalmente grande"])
    if not pd.isna(row["m5_atr_pct_rank"]) and row["m5_atr_pct_rank"] > EXTREME_VOL_PCT:
        return Decision("NO_TRADE", missing=["volatilidade extrema"])

    # 5m pullback into a relevant zone
    extreme = row["m5_pb_lo"] if side == "LONG" else row["m5_pb_hi"]
    leg = row["m5_leg_hi"] if side == "LONG" else row["m5_leg_lo"]
    if (leg - extreme) * sign < MIN_RETRACE * atr5:
        return Decision("NO_TRADE", missing=["sem pullback relevante no 5m"])
    zones = {"EMA21 5m": row["m5_ema21"], "EMA50 5m": row["m5_ema50"], "VWAP": row["vwap"]}
    if side == "LONG":
        zones |= {"suporte 5m": row["m5_sl"], "rompimento anterior": row["m5_sh_prev"]}
    else:
        zones |= {"resistência 5m": row["m5_sh"], "rompimento anterior": row["m5_sl_prev"]}
    touched = {}
    for name, level in zones.items():
        if pd.isna(level):
            continue
        # LONG: extreme came down to within ZONE_TOL above the level and did not break it by > ZONE_BREAK
        dist = (extreme - level) * sign
        if -ZONE_BREAK * atr5 <= dist <= ZONE_TOL * atr5:
            touched[name] = level
    if not touched:
        return Decision("NO_TRADE", missing=["pullback não chegou a uma região relevante"])
    if (row["m5_close"] - (row["m5_ema50"] - sign * ZONE_BREAK * atr5)) * sign < 0:
        return Decision("NO_TRADE", missing=["pullback quebrou a EMA50 do 5m (tendência comprometida)"])
    zone_name, zone = min(touched.items(), key=lambda kv: abs(kv[1] - extreme))

    # 1m confirmation
    recent = f.iloc[i - REJECT_BARS_1M + 1: i + 1]
    touch = (recent["low"].min() - zone <= ZONE_TOL * atr5) if side == "LONG" \
        else (zone - recent["high"].max() <= ZONE_TOL * atr5)
    rejected = touch and (row["close"] - zone) * sign > 0
    rng = row["high"] - row["low"]
    body = (row["close"] - row["open"]) * sign
    strong = rng > 0 and body >= 0.6 * rng and (
        (row["close"] >= row["high"] - 0.3 * rng) if side == "LONG" else (row["close"] <= row["low"] + 0.3 * rng))
    prev = f.iloc[i - MICRO_BARS_1M: i]
    micro_break = (row["close"] > prev["high"].max()) if side == "LONG" else (row["close"] < prev["low"].min())
    volume_ok = row["volume"] >= VOL_MULT * row["vol_avg20"]
    if not rejected:
        missing.append("sem rejeição da região no 1m")
    if not (strong or micro_break):
        missing.append("sem candle de força nem rompimento do micro topo/fundo")
    if not volume_ok:
        missing.append(f"volume {row['volume'] / row['vol_avg20']:.2f}x < 1.2x a média")
    if missing:
        return Decision("NO_TRADE", missing=missing)

    # levels
    entry = row["close"]
    pb_extreme = min(extreme, recent["low"].min()) if side == "LONG" else max(extreme, recent["high"].max())
    stop = pb_extreme - sign * STOP_BUFFER * atr5
    risk = (entry - stop) * sign
    if abs(entry - zone) > MAX_CHASE_ATR * atr5:
        return Decision("NO_TRADE", missing=["preço já se afastou mais de 0.5 ATR da região"])
    if risk < MIN_STOP_ATR * atr5:
        return Decision("NO_TRADE", missing=["stop próximo demais (ruído)"])
    if risk > MAX_STOP_ATR * atr5:
        return Decision("NO_TRADE", missing=["stop distante demais"])
    cost = entry * costs.round_trip_pct()
    tp1 = entry + sign * (2 * (risk + cost) + cost)   # 2R NET of costs
    tp2 = entry + sign * (3 * (risk + cost) + cost)
    net_rr = (abs(tp1 - entry) - cost) / (risk + cost)

    ema_ok = (row["ema9"] > row["ema21"]) if side == "LONG" else (row["ema9"] < row["ema21"])
    vwap_ok = (row["close"] > row["vwap"]) if side == "LONG" else (row["close"] < row["vwap"])
    score = (20 if clear_trend else 10) + (15 if ema_ok else 0) + (15 if vwap_ok else 0) + 15 + 15 + 10 \
        + (10 if net_rr >= 2 - 1e-9 else 0)
    reasons = [f"tendência 15m {'de alta' if side == 'LONG' else 'de baixa'}", f"pullback em {zone_name}",
               "rejeição + " + ("candle de força" if strong else "rompimento do micro " + ("topo" if side == "LONG" else "fundo")),
               f"volume {row['volume'] / row['vol_avg20']:.1f}x a média"]
    if ema_ok:
        reasons.append("EMA9/21 do 1m alinhadas")
    if vwap_ok:
        reasons.append(f"preço {'acima' if side == 'LONG' else 'abaixo'} da VWAP")
    snapshot = {k: float(row[k]) for k in ("ema9", "ema21", "vwap", "atr1", "volume", "vol_avg20",
                                            "m5_atr", "m15_ema9", "m15_ema21", "m15_ema50")}
    if score < MIN_SCORE:
        return Decision("NO_TRADE", score=score, missing=[f"score {score} < {MIN_SCORE}"], snapshot=snapshot)
    return Decision(side, score, entry, stop, tp1, tp2, zone_name, reasons, [], snapshot)
