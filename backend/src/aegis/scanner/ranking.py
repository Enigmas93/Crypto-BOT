"""Market Scanner - Fase 14. Pure ranking logic, no I/O - given a snapshot
of 24h stats, picks which liquid symbols are moving hardest right now.

Deliberately simple momentum metric (`abs(price_change_pct)`), not a
volume-surge-vs-baseline z-score: a proper baseline would need trailing
history this endpoint alone doesn't provide, and inventing one without
evidence it's better would violate spec rule 151 (never claim precision
that doesn't exist). This is a documented starting point for calibration,
not a claim of sophistication.

`min_quote_volume` is the liquidity floor - the user explicitly chose
"liquid pairs only" over "include thin/new listings" (spec section 9's
philosophy: capital preservation first) - this function enforces that
floor structurally, not as an afterthought filter some caller might forget.
"""
from __future__ import annotations

from dataclasses import dataclass

from aegis.providers.binance.models import TickerStats


@dataclass(slots=True)
class MomentumCandidate:
    symbol: str
    price_change_pct: float
    quote_volume: float
    momentum_score: float


def rank_by_momentum(
    tickers: list[TickerStats], min_quote_volume: float,
    exclude: set[str] | None = None, top_n: int = 5,
) -> list[MomentumCandidate]:
    exclude = exclude or set()
    candidates = [
        MomentumCandidate(
            symbol=t.symbol, price_change_pct=t.price_change_pct,
            quote_volume=t.quote_volume, momentum_score=abs(t.price_change_pct),
        )
        for t in tickers
        if t.symbol.endswith("USDT") and t.quote_volume >= min_quote_volume and t.symbol not in exclude
    ]
    candidates.sort(key=lambda c: -c.momentum_score)
    return candidates[:top_n]
