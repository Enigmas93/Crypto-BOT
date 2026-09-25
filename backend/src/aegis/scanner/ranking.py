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

Non-crypto instruments are excluded structurally too (Fase 17e) - found
live 2026-09-25 from a crash report: BingX lists tokenized commodity/FX-
cross contracts (gold, silver, oil, wheat, XAU/EUR, ...) on the SAME
ticker/contract endpoints the momentum scanner reads, all under BingX's own
"NCCO"-prefixed asset naming (e.g. `NCCOGOLD2USD-USDT`, `asset` field
`NCCOGOLD2USD` - verified live against `get_contracts()`, contrasted with a
real pair like BTC-USDT's `asset` field simply being `BTC`). These clear
`min_quote_volume` and `symbol.endswith("USDT")` like any real pair, but
BingX's order-placement endpoint rejects them for a one-way-mode API
account (code 101414: "non-crypto symbol with one-way mode not support
openapi and copy trade") - the scanner had been ranking
`NCCOGOLD2USD-USDT` as its only "momentum" candidate for a stretch, with a
momentum_score of exactly 0.0 (no real price movement, just clearing the
volume floor), and attempting to trade it crashed the whole process before
Fase 17d's BracketOpenError fix. Filtering here, not per-exchange at the
call site, keeps every future scanner-consuming engine (Binance or BingX)
safe by construction rather than by remembering to filter upstream.
"""
from __future__ import annotations

from dataclasses import dataclass

from aegis.providers.binance.models import TickerStats

_NON_CRYPTO_ASSET_PREFIX = "NCCO"


def _is_crypto_symbol(symbol: str) -> bool:
    return not symbol.startswith(_NON_CRYPTO_ASSET_PREFIX)


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
        and _is_crypto_symbol(t.symbol)
    ]
    candidates.sort(key=lambda c: -c.momentum_score)
    return candidates[:top_n]
