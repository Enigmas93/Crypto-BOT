"""PortfolioCorrelationEngine (Fase 17) - the missing piece behind
RiskEngine's CORRELATED_EXPOSURE check. `AccountState.correlated_exposure_pct`
has existed since Phase 7 and `RiskEngine.evaluate()` has always compared it
against `max_correlated_exposure_pct` (default 0.30), but nothing ever
computed it - it sat at its 0.0 dataclass default forever, so the check
never once fired for any account. Found during the Fase 17 audit, flagged
to the user as needing a design decision (this isn't a wiring bug like
`open_positions_count` was - it's a genuinely unbuilt feature), and built
here once the user confirmed they wanted the full blueprint gap list closed.

Methodology (a documented HYPOTHESIS - spec section 54's own standard
applies here too: weights/rules earn their place through backtesting,
never assumed correct up front): pairwise Pearson correlation of recent
percentage returns between every pair of symbols with an open position.
Two symbols are "correlated" if |correlation| >= correlation_threshold.
`correlated_exposure_pct` is the fraction of open positions that share a
correlated pair with at least one other open position - a concentration
measure (how much of the book is sitting in one correlated cluster), not a
full covariance/beta-weighted portfolio risk model. A single open position
is trivially 0% correlated (nothing to correlate against yet).
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

DEFAULT_LOOKBACK_CANDLES = 100
DEFAULT_CORRELATION_THRESHOLD = 0.70
_MIN_OVERLAPPING_POINTS = 5  # fewer than this and a correlation coefficient is noise, not signal


@dataclass(slots=True)
class CorrelationResult:
    correlated_exposure_pct: float  # 0.0-1.0, same scale as max_correlated_exposure_pct
    correlated_pairs: list[tuple[str, str, float]]  # (symbol_a, symbol_b, correlation) - for logging/audit


def compute_correlated_exposure(
    returns_by_symbol: dict[str, pd.Series], correlation_threshold: float = DEFAULT_CORRELATION_THRESHOLD,
) -> CorrelationResult:
    symbols = list(returns_by_symbol.keys())
    if len(symbols) < 2:
        return CorrelationResult(correlated_exposure_pct=0.0, correlated_pairs=[])

    correlated_pairs: list[tuple[str, str, float]] = []
    correlated_symbols: set[str] = set()
    for i in range(len(symbols)):
        for j in range(i + 1, len(symbols)):
            a, b = symbols[i], symbols[j]
            series_a, series_b = returns_by_symbol[a].align(returns_by_symbol[b], join="inner")
            if len(series_a) < _MIN_OVERLAPPING_POINTS:
                continue  # not enough overlapping history to trust a correlation between these two
            corr = series_a.corr(series_b)
            if corr is not None and not pd.isna(corr) and abs(corr) >= correlation_threshold:
                correlated_pairs.append((a, b, round(float(corr), 3)))
                correlated_symbols.add(a)
                correlated_symbols.add(b)

    return CorrelationResult(
        correlated_exposure_pct=round(len(correlated_symbols) / len(symbols), 4),
        correlated_pairs=correlated_pairs,
    )


async def fetch_returns_from_candle_repo(
    candle_repo, symbols: list[str], interval: str, lookback: int = DEFAULT_LOOKBACK_CANDLES,
) -> dict[str, pd.Series]:
    """For Paper/Shadow (fixed symbol universe, always persisted by the
    collector) - reads closed candles already in Postgres, no exchange call."""
    returns: dict[str, pd.Series] = {}
    for symbol in symbols:
        df = await candle_repo.fetch_ohlcv(symbol, interval, limit=lookback, closed_only=True)
        if len(df) < _MIN_OVERLAPPING_POINTS + 1:  # pct_change drops the first row
            continue
        returns[symbol] = df["close"].pct_change().dropna().reset_index(drop=True)
    return returns


async def fetch_returns_from_rest_klines(
    rest_client, symbols: list[str], interval: str, lookback: int = DEFAULT_LOOKBACK_CANDLES,
) -> dict[str, pd.Series]:
    """For Momentum (dynamic scanner-picked universe, not guaranteed to be
    persisted by the collector) - reads candles directly from whichever
    exchange REST client the engine already uses (Binance or BingX both
    implement get_klines with the same Kline shape). Sorted explicitly by
    close_time_ms since the two exchanges don't return the same order
    (BingX is newest-first, Binance oldest-first - see
    aegis.providers.bingx.rest_client's module docstring)."""
    returns: dict[str, pd.Series] = {}
    for symbol in symbols:
        klines = await rest_client.get_klines(symbol, interval, limit=lookback)
        if len(klines) < _MIN_OVERLAPPING_POINTS + 1:
            continue
        closes = pd.Series([k.close for k in sorted(klines, key=lambda k: k.close_time_ms)])
        returns[symbol] = closes.pct_change().dropna().reset_index(drop=True)
    return returns


async def compute_account_correlated_exposure_from_candles(
    candle_repo, open_symbols: list[str], interval: str,
    lookback: int = DEFAULT_LOOKBACK_CANDLES, correlation_threshold: float = DEFAULT_CORRELATION_THRESHOLD,
) -> CorrelationResult:
    if len(open_symbols) < 2:
        return CorrelationResult(correlated_exposure_pct=0.0, correlated_pairs=[])
    returns = await fetch_returns_from_candle_repo(candle_repo, open_symbols, interval, lookback)
    return compute_correlated_exposure(returns, correlation_threshold)


async def compute_account_correlated_exposure_from_rest(
    rest_client, open_symbols: list[str], interval: str,
    lookback: int = DEFAULT_LOOKBACK_CANDLES, correlation_threshold: float = DEFAULT_CORRELATION_THRESHOLD,
) -> CorrelationResult:
    if len(open_symbols) < 2:
        return CorrelationResult(correlated_exposure_pct=0.0, correlated_pairs=[])
    returns = await fetch_returns_from_rest_klines(rest_client, open_symbols, interval, lookback)
    return compute_correlated_exposure(returns, correlation_threshold)
