"""DerivativesAnalysisService - spec sections 27/28.

Mirrors `technical/service.py`'s split: `compute_snapshot` is a pure
function (DataFrames in, DerivativesSnapshot out) so the PRICE×OI pattern
logic and the z-score math are testable with synthetic data. The polling
half (`DerivativesEngine`) is the only part that touches the network/DB.

Spec section 28 is explicit that these patterns are *features*, not
signals: "Utilizar esses padrões como features. Não interpretar
automaticamente como BUY ou SELL." Nothing here produces a trade decision.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime

import pandas as pd

from aegis import stats as analytics
from aegis.logging_utils import get_logger, log_event
from aegis.providers.binance.models import LongShortRatioPoint, OpenInterestHistPoint

_LOG = get_logger("derivatives.service")

MIN_POINTS_FOR_FULL_HISTORY = 30


@dataclass(slots=True)
class DerivativesSnapshot:
    symbol: str
    period: str
    as_of: datetime | None
    data_points: int
    quality: str  # NO_DATA | PARTIAL_HISTORY | OK

    open_interest: float | None = None
    open_interest_value: float | None = None
    oi_change_pct: float | None = None
    oi_acceleration: float | None = None
    oi_to_volume_ratio: float | None = None

    price_change_pct: float | None = None
    price_oi_pattern: str | None = None  # PRICE_UP_OI_UP | PRICE_UP_OI_DOWN | PRICE_DOWN_OI_UP | PRICE_DOWN_OI_DOWN

    global_long_short_ratio: float | None = None
    global_long_short_ratio_zscore: float | None = None
    top_account_long_short_ratio: float | None = None
    top_account_long_short_ratio_zscore: float | None = None
    top_position_long_short_ratio: float | None = None
    top_position_long_short_ratio_zscore: float | None = None

    funding_rate: float | None = None
    funding_zscore: float | None = None
    basis: float | None = None
    basis_pct: float | None = None

    def to_features_dict(self) -> dict:
        d = asdict(self)
        for key in ("symbol", "period", "as_of", "data_points", "quality"):
            d.pop(key, None)
        return {k: v for k, v in d.items() if v is not None}


def _price_oi_pattern(price_change_pct: float | None, oi_change_pct: float | None) -> str | None:
    if price_change_pct is None or oi_change_pct is None:
        return None
    price_up = price_change_pct > 0
    oi_up = oi_change_pct > 0
    if price_up and oi_up:
        return "PRICE_UP_OI_UP"
    if price_up and not oi_up:
        return "PRICE_UP_OI_DOWN"
    if not price_up and oi_up:
        return "PRICE_DOWN_OI_UP"
    return "PRICE_DOWN_OI_DOWN"


def compute_snapshot(
    symbol: str,
    period: str,
    oi_df: pd.DataFrame,
    global_ls_df: pd.DataFrame,
    top_account_ls_df: pd.DataFrame,
    top_position_ls_df: pd.DataFrame,
    funding_df: pd.DataFrame,
    price_df: pd.DataFrame,
) -> DerivativesSnapshot:
    if oi_df.empty:
        return DerivativesSnapshot(symbol=symbol, period=period, as_of=None, data_points=0, quality="NO_DATA")

    oi = oi_df["sum_open_interest"]
    oi_change = analytics.latest_pct_change(oi)
    oi_value_latest = float(oi_df["sum_open_interest_value"].iloc[-1])

    price_change = None
    oi_to_volume = None
    if not price_df.empty:
        price_change = analytics.latest_pct_change(price_df["close"])
        latest_volume = float(price_df["quote_volume"].iloc[-1])
        if latest_volume:
            oi_to_volume = oi_value_latest / latest_volume

    funding_rate = None
    funding_zscore = None
    basis = None
    basis_pct = None
    if not funding_df.empty:
        funding_rate = float(funding_df["funding_rate"].iloc[-1])
        funding_zscore = analytics.latest_zscore(funding_df["funding_rate"])
        mark = float(funding_df["mark_price"].iloc[-1])
        index = float(funding_df["index_price"].iloc[-1])
        if index:
            basis = mark - index
            basis_pct = basis / index * 100

    def _ratio_fields(df: pd.DataFrame) -> tuple[float | None, float | None]:
        if df.empty:
            return None, None
        return float(df["long_short_ratio"].iloc[-1]), analytics.latest_zscore(df["long_short_ratio"])

    global_ratio, global_z = _ratio_fields(global_ls_df)
    top_account_ratio, top_account_z = _ratio_fields(top_account_ls_df)
    top_position_ratio, top_position_z = _ratio_fields(top_position_ls_df)

    quality = "OK" if len(oi_df) >= MIN_POINTS_FOR_FULL_HISTORY else "PARTIAL_HISTORY"

    return DerivativesSnapshot(
        symbol=symbol,
        period=period,
        as_of=pd.Timestamp(oi_df["time"].iloc[-1]).to_pydatetime(),
        data_points=len(oi_df),
        quality=quality,
        open_interest=float(oi.iloc[-1]),
        open_interest_value=oi_value_latest,
        oi_change_pct=oi_change,
        oi_acceleration=analytics.acceleration(oi),
        oi_to_volume_ratio=oi_to_volume,
        price_change_pct=price_change,
        price_oi_pattern=_price_oi_pattern(price_change, oi_change),
        global_long_short_ratio=global_ratio,
        global_long_short_ratio_zscore=global_z,
        top_account_long_short_ratio=top_account_ratio,
        top_account_long_short_ratio_zscore=top_account_z,
        top_position_long_short_ratio=top_position_ratio,
        top_position_long_short_ratio_zscore=top_position_z,
        funding_rate=funding_rate,
        funding_zscore=funding_zscore,
        basis=basis,
        basis_pct=basis_pct,
    )


class DerivativesEngine:
    """Polls Binance REST for what has no WebSocket stream (OI, long/short
    ratios), persists the raw series, then computes and merges a
    DerivativesSnapshot into `market_features` for each (symbol, period).
    """

    def __init__(self, rest_client, derivatives_repo, candle_repo, feature_repo, settings) -> None:
        self.rest = rest_client
        self.derivatives_repo = derivatives_repo
        self.candle_repo = candle_repo
        self.feature_repo = feature_repo
        self.settings = settings

    async def poll_and_persist_raw(self, symbol: str, period: str) -> None:
        limit = self.settings.derivatives_hist_limit
        oi_raw = await self.rest.get_open_interest_hist(symbol, period=period, limit=limit)
        oi_points = [OpenInterestHistPoint.from_rest_payload(symbol, period, row) for row in oi_raw]
        await self.derivatives_repo.insert_open_interest(oi_points)

        for ratio_type, fetcher in (
            ("GLOBAL_ACCOUNT", self.rest.get_global_long_short_ratio),
            ("TOP_ACCOUNT", self.rest.get_top_long_short_account_ratio),
            ("TOP_POSITION", self.rest.get_top_long_short_position_ratio),
        ):
            raw = await fetcher(symbol, period=period, limit=limit)
            points = [LongShortRatioPoint.from_rest_payload(symbol, ratio_type, period, row) for row in raw]
            await self.derivatives_repo.insert_long_short_ratios(points)

    async def compute_and_store_snapshot(self, symbol: str, period: str) -> DerivativesSnapshot:
        limit = self.settings.derivatives_hist_limit
        oi_df = await self.derivatives_repo.fetch_open_interest_series(symbol, period, limit)
        global_df = await self.derivatives_repo.fetch_long_short_series(symbol, "GLOBAL_ACCOUNT", period, limit)
        top_acct_df = await self.derivatives_repo.fetch_long_short_series(symbol, "TOP_ACCOUNT", period, limit)
        top_pos_df = await self.derivatives_repo.fetch_long_short_series(symbol, "TOP_POSITION", period, limit)
        funding_df = await self.derivatives_repo.fetch_funding_series(symbol, self.settings.funding_zscore_lookback)
        price_df = await self.candle_repo.fetch_ohlcv(symbol, period, limit=limit, closed_only=True)

        snapshot = compute_snapshot(symbol, period, oi_df, global_df, top_acct_df, top_pos_df, funding_df, price_df)
        await self.feature_repo.upsert_snapshot(snapshot)
        return snapshot

    async def run_once(self) -> dict[str, DerivativesSnapshot]:
        results: dict[str, DerivativesSnapshot] = {}
        for symbol in self.settings.symbols:
            for period in self.settings.derivatives_periods:
                try:
                    await self.poll_and_persist_raw(symbol, period)
                    snapshot = await self.compute_and_store_snapshot(symbol, period)
                    results[f"{symbol}:{period}"] = snapshot
                except Exception as exc:  # noqa: BLE001 - one bad (symbol, period) must not kill the poll cycle
                    log_event(
                        _LOG, "derivatives_poll_failed", level=40,
                        symbol=symbol, period=period, error=str(exc),
                    )
        return results
