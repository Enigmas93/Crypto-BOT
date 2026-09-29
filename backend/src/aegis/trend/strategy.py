"""BTC+ETH daily trend (Fase 24) - the only style that survived the Fase 23/24
research battery (scalping, intraday momentum/reversal, grid, pairs
stat-arb, RSI pullbacks, cash-and-carry and more were all rejected).

Once per day, on the closed 00:00 UTC daily bar:
  signal_s = mean over L in LOOKBACKS of sign(close_t / close_{t-L} - 1)
  (so +1 when every lookback agrees on up, 0.2 steps in between), negative
  signals scaled by SHORT_SCALE (crypto's long-run drift makes shorts pay
  less, so they run at half size), then inverse-volatility weights so BTC
  and ETH contribute similar risk:
  weight_s = signal_s * (1/vol_s) / sum(1/vol)   ->  gross exposure <= 1x.

Evidence (Binance perps 2019-11..2026-09, 0.08%/side + real funding):
Sharpe 1.01, CAGR 39%, max DD -42%; with doubled costs Sharpe 0.94; one
day late execution 0.86. Every single lookback 30-60d positive on Binance
2023-26 AND on BingX-native 2025-26. Weak years: 2022 -15%, 2024 0%. It is
a trend follower: it loses in choppy markets and makes its money in a few
large moves - expect long flat or losing stretches.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

TREND_SYMBOLS = ("BTCUSDT", "ETHUSDT")
LOOKBACKS = (20, 30, 40, 50, 60)
SHORT_SCALE = 0.5
VOL_WINDOW = 20


@dataclass(slots=True)
class SymbolSignal:
    symbol: str
    votes: dict[int, int]     # lookback (days) -> +1 / -1 / 0
    signal: float             # after short scaling, in [-SHORT_SCALE, 1]
    daily_vol: float
    weight: float             # target fraction of equity (signed)


def required_bars(lookbacks: tuple[int, ...] = LOOKBACKS, vol_window: int = VOL_WINDOW) -> int:
    return max(max(lookbacks), vol_window) + 1


def _sign(x: float) -> int:
    return 1 if x > 0 else (-1 if x < 0 else 0)


def _stdev(xs: list[float]) -> float:
    mean = sum(xs) / len(xs)
    return math.sqrt(sum((x - mean) ** 2 for x in xs) / (len(xs) - 1))   # ddof=1, same as pandas


def target_weights(closes: dict[str, list[float]], lookbacks: tuple[int, ...] = LOOKBACKS,
                   short_scale: float = SHORT_SCALE, vol_window: int = VOL_WINDOW) -> dict[str, SymbolSignal]:
    """`closes[symbol]` = closed daily closes, oldest first, last = the bar
    just closed. Raises ValueError when history is too short - trading on a
    partial signal would silently change the strategy."""
    need = required_bars(lookbacks, vol_window)
    raw: dict[str, tuple[dict[int, int], float, float]] = {}
    for symbol, c in closes.items():
        if len(c) < need:
            raise ValueError(f"{symbol}: {len(c)} daily closes, need {need}")
        votes = {L: _sign(c[-1] / c[-1 - L] - 1) for L in lookbacks}
        sig = sum(votes.values()) / len(lookbacks)
        if sig < 0:
            sig *= short_scale
        rets = [c[i] / c[i - 1] - 1 for i in range(len(c) - vol_window, len(c))]
        raw[symbol] = (votes, sig, _stdev(rets))
    inv_total = sum(1 / v for _, _, v in raw.values() if v > 0)
    out = {}
    for symbol, (votes, sig, vol) in raw.items():
        w = sig * (1 / vol) / inv_total if vol > 0 and inv_total > 0 else 0.0
        out[symbol] = SymbolSignal(symbol, votes, sig, vol, w)
    return out
