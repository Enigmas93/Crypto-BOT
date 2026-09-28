#!/usr/bin/env python
"""Reproduces the Fase 18 strategy research and stores the evidence the
dashboard shows next to each configuration (table strategy_research_runs).

Data: public klines only - BingX-native 1h for every liquid BingX crypto
pair (>= $10M 24h volume, ~21 months of history available) and Binance
USDT-M 1h/15m for 20 liquid pairs (2y / 1y). Costs: 0.05% taker fee per
side plus slippage (0.03% Shadow, 0.05% Momentum). Split: first 60% of
each series in-sample, last 40% out-of-sample.

Downloads are cached under backend/.research_cache (gitignored), so re-runs
only fetch what is missing.

Usage (from backend/, venv active):
    python scripts/run_strategy_research.py            # run + store
    python scripts/run_strategy_research.py --no-store # just print
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

import httpx
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aegis.config import get_settings  # noqa: E402
from aegis.db.ai_repository import AiRepository  # noqa: E402
from aegis.db.engine import close_pool, create_pool  # noqa: E402
from aegis.research.vectorized import ExitRule, decisions, features, simulate, summarize  # noqa: E402

CACHE = Path(__file__).resolve().parents[1] / ".research_cache"
BINANCE_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "1000PEPEUSDT", "NEARUSDT", "BNBUSDT",
                   "ADAUSDT", "AVAXUSDT", "LINKUSDT", "SUIUSDT", "LTCUSDT", "DOTUSDT", "WIFUSDT", "ARBUSDT", "OPUSDT",
                   "APTUSDT", "INJUSDT", "TIAUSDT"]
NON_CRYPTO = ("NCCO", "NCSK", "NCSI", "NCFX")
MS = {"15m": 900_000, "1h": 3_600_000}
OLD = ("TREND_PULLBACK", "BREAKOUT", "MEAN_REVERSION")
NEW = OLD + ("DONCHIAN_TREND",)

SHADOW_SCENARIOS = [
    ("Antiga: TP+BO+MR, stop 2 ATR, alvo 2R", OLD, ExitRule(2.0, 2.0)),
    ("Nova (ativa): +DONCHIAN, stop 2 ATR, alvo 3R", NEW, ExitRule(2.0, 3.0)),
    ("Nova + breakeven em +0.75R", NEW, ExitRule(2.0, 3.0, be_trigger_r=0.75)),
    ("Nova + breakeven em +0.5R", NEW, ExitRule(2.0, 3.0, be_trigger_r=0.5, be_offset_r=0.05)),
    ("Nova + trailing 3 ATR sem alvo", NEW, ExitRule(2.0, None, trail_trigger_r=0.0, trail_atr=3.0)),
]
MOMENTUM_SCENARIOS = [
    ("Momentum antigo: trailing 1.33 ATR", OLD, ExitRule(2.0, None, trail_trigger_r=0.5, trail_atr=1.33), False),
    ("Momentum novo (ativo): alvo 2R + direção alinhada", NEW, ExitRule(2.0, 2.0), True),
]


# -- data -----------------------------------------------------------------
def _frame(rows: list[tuple], interval: str) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=["t", "open", "high", "low", "close", "volume"]).drop_duplicates("t").sort_values("t")
    df["open_time"] = pd.to_datetime(df["t"], unit="ms", utc=True)
    df["close_time"] = df["open_time"] + pd.Timedelta(milliseconds=MS[interval] - 1)
    df = df[df["close_time"] < pd.Timestamp.now(tz="UTC")]
    return df.drop(columns="t").reset_index(drop=True)


def load_binance(c: httpx.Client, symbol: str, interval: str, days: int) -> pd.DataFrame:
    path = CACHE / "binance" / f"{symbol}_{interval}.pkl"
    if path.exists():
        return pd.read_pickle(path)
    end = int(time.time() * 1000)
    cur, rows = end - days * 86_400_000, []
    while cur < end:
        data = c.get("https://fapi.binance.com/fapi/v1/klines",
                     params={"symbol": symbol, "interval": interval, "startTime": cur, "limit": 1500}).json()
        if not data:
            break
        rows += [(int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])) for k in data]
        cur = int(data[-1][0]) + MS[interval]
        if len(data) < 1500:
            break
    df = _frame(rows, interval)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_pickle(path)
    return df


def load_bingx(c: httpx.Client, symbol: str, interval: str, days: int = 640) -> pd.DataFrame:
    path = CACHE / "bingx" / f"{symbol}_{interval}.pkl"
    if path.exists():
        return pd.read_pickle(path)
    stop_ms = int(time.time() * 1000) - days * 86_400_000
    end, rows = int(time.time() * 1000), {}
    while end > stop_ms:
        data = c.get("https://open-api.bingx.com/openApi/swap/v3/quote/klines",
                     params={"symbol": f"{symbol[:-4]}-USDT", "interval": interval, "endTime": end,
                             "limit": 1000}).json().get("data") or []
        if not data:
            break
        for k in data:
            rows[int(k["time"])] = (int(k["time"]), float(k["open"]), float(k["high"]), float(k["low"]),
                                    float(k["close"]), float(k["volume"]))
        oldest = min(int(k["time"]) for k in data)
        if oldest >= end:
            break
        end = oldest - 1
        time.sleep(0.15)
    df = _frame(list(rows.values()), interval)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_pickle(path)
    return df


def bingx_universe(c: httpx.Client, min_volume: float) -> list[str]:
    tickers = c.get("https://open-api.bingx.com/openApi/swap/v2/quote/ticker").json()["data"]
    return [t["symbol"].replace("-", "") for t in sorted(tickers, key=lambda t: -float(t["quoteVolume"]))
            if t["symbol"].endswith("-USDT") and not t["symbol"].startswith(NON_CRYPTO)
            and float(t["quoteVolume"]) >= min_volume]


# -- evaluation --------------------------------------------------------------
def _metrics(per_symbol: dict[str, list], fee_note: str) -> dict:
    all_t = [t for ts in per_symbol.values() for t in ts]
    if not all_t:
        return {"note": "sem trades"}
    times = sorted(t.time for t in all_t)
    split = times[int(len(times) * 0.6)]
    is_r = [t.r for t in all_t if t.time < split]
    oos_r = [t.r for t in all_t if t.time >= split]
    halves: dict[str, list[float]] = {}
    for t in all_t:
        halves.setdefault(f"{t.time.year}H{1 if t.time.month <= 6 else 2}", []).append(t.r)
    return {
        "all": summarize([t.r for t in all_t]), "in_sample": summarize(is_r), "out_of_sample": summarize(oos_r),
        "oos_starts": split.isoformat(),
        "symbols": len(per_symbol), "symbols_positive": sum(1 for ts in per_symbol.values() if sum(t.r for t in ts) > 0),
        "by_half_year": {k: round(float(np.mean(v)), 4) for k, v in sorted(halves.items()) if len(v) >= 30},
        "costs": fee_note,
    }


def run_shadow(frames: dict[str, pd.DataFrame]) -> list[tuple[str, dict]]:
    feats = {s: features(df) for s, df in frames.items() if len(df) > 400}
    out = []
    for name, ids, rule in SHADOW_SCENARIOS:
        per_symbol = {s: simulate(f, decisions(f, ids), rule) for s, f in feats.items()}
        out.append((name, _metrics(per_symbol, "taxa 0.05%/lado + slippage 0.03%")))
    return out


def run_momentum(frames: dict[str, pd.DataFrame], top_n: int = 5) -> list[tuple[str, dict]]:
    feats = {s: features(df) for s, df in frames.items() if len(df) > 400}
    panel = pd.DataFrame({s: f.set_index("open_time")["close"] for s, f in feats.items()}).sort_index()
    change = panel / panel.shift(24) - 1
    rank = change.abs().rank(axis=1, ascending=False)
    out = []
    for name, ids, rule, aligned in MOMENTUM_SCENARIOS:
        per_symbol = {}
        for s, f in feats.items():
            member = (rank[s].reindex(f["open_time"]) <= top_n).fillna(False).to_numpy()
            ext = (f["close"] - f["close"].shift(8)).abs() / f["atr"]
            fresh = (((ext <= 3.0) | ext.isna()) & ((f["volz"] <= 8.0) | f["volz"].isna())).to_numpy()
            dec = decisions(f, ids)
            if aligned:
                direction = np.sign(change[s].reindex(f["open_time"]).fillna(0).to_numpy())
                dec = np.where(dec == direction, dec, 0).astype(np.int8)
            per_symbol[s] = simulate(f, dec, rule, slip=0.0005, allowed=member & fresh)
        out.append((name, _metrics(per_symbol, "taxa 0.05%/lado + slippage 0.05%")))
    return out


async def _store(results: list[tuple[str, str, str, dict, dict]]) -> None:
    pool = await create_pool(get_settings())
    try:
        repo = AiRepository(pool)
        for name, source, interval, config, metrics in results:
            await repo.insert_research_run(name, source, interval, config, metrics)
    finally:
        await close_pool(pool)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-store", action="store_true")
    ap.add_argument("--bingx-min-volume", type=float, default=10e6)
    args = ap.parse_args()
    results = []
    with httpx.Client(timeout=30) as c:
        bingx = {s: load_bingx(c, s, "1h") for s in bingx_universe(c, args.bingx_min_volume)}
        binance_1h = {s: load_binance(c, s, "1h", 730) for s in BINANCE_SYMBOLS}
        binance_15m = {s: load_binance(c, s, "15m", 365) for s in BINANCE_SYMBOLS}
    for source, interval, frames in (("BINGX", "1h", bingx), ("BINANCE", "1h", binance_1h),
                                     ("BINANCE", "15m", binance_15m)):
        for name, metrics in run_shadow(frames):
            results.append((f"SHADOW · {name}", source, interval, {"engine": "shadow"}, metrics))
    for name, metrics in run_momentum(bingx):
        results.append((f"MOMENTUM · {name}", "BINGX", "1h", {"engine": "momentum", "top_n": 5}, metrics))
    for name, source, interval, _, m in results:
        o = m.get("out_of_sample", {})
        print(f"{source:8s} {interval:4s} {name:62s} trades={m.get('all', {}).get('trades')} "
              f"avgR_all={m.get('all', {}).get('avg_r')} avgR_OOS={o.get('avg_r')} "
              f"sym+={m.get('symbols_positive')}/{m.get('symbols')}")
    if not args.no_store:
        asyncio.run(_store(results))
        print("stored", len(results), "research runs")


if __name__ == "__main__":
    main()
