"""Read-only lookups of already-computed market features for a single
symbol/cycle - built to let STRATEGY_LIQUIDATION_SQUEEZE (Fase 17e) run
with REAL data inside Paper/Shadow Trading, not just backtests.

Reuses the exact same pure `compute_snapshot` functions
DerivativesEngine/LiquidationEngine themselves call (aegis.derivatives.
service/aegis.liquidation.service) - this is deliberately NOT a persisting
operation (no feature_repo write): a trading engine asking "what's the
current derivatives/liquidation picture for this symbol" during its own
decision cycle should never have a side effect on the persisted features
table, which is DerivativesEngine/LiquidationEngine's own job on their own
polling schedule.

Scoped to Binance only (Paper/Shadow's real account, not Momentum's
dynamic universe or any BingX account) - `settings.symbols` is the fixed
universe DerivativesEngine/LiquidationEngine actually collect for; a
symbol outside that list (BingX's shadow_bingx/momentum_bingx, or
Momentum's scanner-picked candidates) has no data here, so callers for
those get `None` back and STRATEGY_LIQUIDATION_SQUEEZE correctly stays
inert (NO_TRADE) for them - a real, disclosed limitation until BingX-side
derivatives/liquidation collection exists, not a bug.
"""
from __future__ import annotations

from aegis.derivatives.service import DerivativesSnapshot
from aegis.derivatives.service import compute_snapshot as compute_derivatives_snapshot
from aegis.liquidation.service import LiquidationSnapshot
from aegis.liquidation.service import compute_snapshot as compute_liquidation_snapshot


async def fetch_derivatives_snapshot(
    derivatives_repo, candle_repo, symbol: str, period: str, funding_lookback: int, hist_limit: int,
) -> DerivativesSnapshot:
    oi_df = await derivatives_repo.fetch_open_interest_series(symbol, period, hist_limit)
    global_df = await derivatives_repo.fetch_long_short_series(symbol, "GLOBAL_ACCOUNT", period, hist_limit)
    top_acct_df = await derivatives_repo.fetch_long_short_series(symbol, "TOP_ACCOUNT", period, hist_limit)
    top_pos_df = await derivatives_repo.fetch_long_short_series(symbol, "TOP_POSITION", period, hist_limit)
    funding_df = await derivatives_repo.fetch_funding_series(symbol, funding_lookback)
    price_df = await candle_repo.fetch_ohlcv(symbol, period, limit=hist_limit, closed_only=True)
    return compute_derivatives_snapshot(symbol, period, oi_df, global_df, top_acct_df, top_pos_df, funding_df, price_df)


async def fetch_liquidation_snapshot(
    liquidation_repo, symbol: str, window_seconds: int, num_buckets: int,
) -> LiquidationSnapshot:
    df = await liquidation_repo.fetch_windowed_stats(symbol, window_seconds, num_buckets)
    period = f"{window_seconds // 60}m" if window_seconds % 60 == 0 else f"{window_seconds}s"
    return compute_liquidation_snapshot(symbol, period, df)
