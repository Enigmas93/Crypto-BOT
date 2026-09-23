"""TechnicalAnalysisService - spec section 25/26.

`compute_snapshot` is a pure function (DataFrame in, TechnicalSnapshot out)
so indicator logic is testable with synthetic data and no database.
`TechnicalAnalysisService` is the thin async layer that pulls candles for
every configured timeframe and calls it - the multi-timeframe part of spec
section 26.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Protocol

import pandas as pd

from aegis.technical import indicators as ta
from aegis.technical.market_structure import analyze_market_structure

MIN_CANDLES_FOR_FULL_HISTORY = 210  # covers EMA200 with a small buffer


class CandleSource(Protocol):
    async def fetch_ohlcv(
        self, symbol: str, interval: str, limit: int = 500, closed_only: bool = True
    ) -> pd.DataFrame: ...


@dataclass(slots=True)
class TechnicalSnapshot:
    symbol: str
    interval: str
    as_of: datetime | None
    data_points: int
    quality: str  # NO_DATA | PARTIAL_HISTORY | OK

    close: float | None = None
    ema_20: float | None = None
    ema_50: float | None = None
    ema_100: float | None = None
    ema_200: float | None = None
    sma_20: float | None = None
    rsi_14: float | None = None
    atr_14: float | None = None
    adx_14: float | None = None
    vwap: float | None = None
    bb_mid: float | None = None
    bb_upper: float | None = None
    bb_lower: float | None = None
    macd: float | None = None
    macd_signal: float | None = None
    macd_hist: float | None = None
    roc_12: float | None = None
    momentum_10: float | None = None
    volume_zscore_20: float | None = None
    volatility_percentile_100: float | None = None
    distance_from_ema200_pct: float | None = None
    distance_from_vwap_pct: float | None = None

    market_structure_trend: str = "UNKNOWN"
    last_swing_high: float | None = None
    last_swing_low: float | None = None
    breakout: bool = False
    breakdown: bool = False

    def to_features_dict(self) -> dict:
        """Everything except the identity columns (symbol/interval/as_of),
        ready to merge into the `market_features` JSONB payload.

        `None` values are dropped, not sent as JSON `null` - the merge in
        `FeatureRepository` is a shallow `jsonb ||`, so a `null` here would
        clobber a real value another engine already wrote for this bar
        (e.g. this snapshot only updates `atr_14`; it must not blank out
        `close`/`rsi_14` a previous write already stored).
        """
        d = asdict(self)
        for key in ("symbol", "interval", "as_of", "data_points", "quality"):
            d.pop(key, None)  # quality already has its own column, not a feature
        return {k: v for k, v in d.items() if v is not None}


def _last_or_none(series: pd.Series) -> float | None:
    if series.empty:
        return None
    value = series.iloc[-1]
    return None if pd.isna(value) else float(value)


def compute_snapshot(symbol: str, interval: str, df: pd.DataFrame) -> TechnicalSnapshot:
    if df.empty:
        return TechnicalSnapshot(symbol=symbol, interval=interval, as_of=None, data_points=0, quality="NO_DATA")

    df = df.sort_values("open_time").reset_index(drop=True)
    close = df["close"]
    last_close = float(close.iloc[-1])

    ema20, ema50, ema100, ema200 = (ta.ema(close, p) for p in (20, 50, 100, 200))
    sma20 = ta.sma(close, 20)
    rsi14 = ta.rsi(close, 14)
    atr14 = ta.atr(df, 14)
    adx14 = ta.adx(df, 14)
    vwap = ta.daily_anchored_vwap(df)
    bb_mid, bb_upper, bb_lower = ta.bollinger_bands(close, 20, 2.0)
    macd_line, macd_signal, macd_hist = ta.macd(close)
    roc12 = ta.roc(close, 12)
    mom10 = ta.momentum(close, 10)
    vol_z20 = ta.volume_zscore(df["volume"], 20)
    vol_pct100 = ta.percentile_rank(atr14, 100)

    ema200_last = _last_or_none(ema200)
    vwap_last = _last_or_none(vwap)
    dist_ema200 = None if ema200_last in (None, 0) else (last_close - ema200_last) / ema200_last * 100
    dist_vwap = None if vwap_last in (None, 0) else (last_close - vwap_last) / vwap_last * 100

    structure = analyze_market_structure(df)
    quality = "OK" if len(df) >= MIN_CANDLES_FOR_FULL_HISTORY else "PARTIAL_HISTORY"

    return TechnicalSnapshot(
        symbol=symbol,
        interval=interval,
        as_of=pd.Timestamp(df["close_time"].iloc[-1]).to_pydatetime(),
        data_points=len(df),
        quality=quality,
        close=last_close,
        ema_20=_last_or_none(ema20),
        ema_50=_last_or_none(ema50),
        ema_100=_last_or_none(ema100),
        ema_200=ema200_last,
        sma_20=_last_or_none(sma20),
        rsi_14=_last_or_none(rsi14),
        atr_14=_last_or_none(atr14),
        adx_14=_last_or_none(adx14),
        vwap=vwap_last,
        bb_mid=_last_or_none(bb_mid),
        bb_upper=_last_or_none(bb_upper),
        bb_lower=_last_or_none(bb_lower),
        macd=_last_or_none(macd_line),
        macd_signal=_last_or_none(macd_signal),
        macd_hist=_last_or_none(macd_hist),
        roc_12=_last_or_none(roc12),
        momentum_10=_last_or_none(mom10),
        volume_zscore_20=_last_or_none(vol_z20),
        volatility_percentile_100=_last_or_none(vol_pct100),
        distance_from_ema200_pct=dist_ema200,
        distance_from_vwap_pct=dist_vwap,
        market_structure_trend=structure.trend,
        last_swing_high=structure.last_swing_high,
        last_swing_low=structure.last_swing_low,
        breakout=structure.breakout,
        breakdown=structure.breakdown,
    )


class TechnicalAnalysisService:
    def __init__(self, candle_source: CandleSource, intervals: list[str], bootstrap_limit: int = 500) -> None:
        self.candle_source = candle_source
        self.intervals = intervals
        self.bootstrap_limit = bootstrap_limit

    async def compute_for_symbol(
        self, symbol: str, intervals: list[str] | None = None
    ) -> dict[str, TechnicalSnapshot]:
        result: dict[str, TechnicalSnapshot] = {}
        for interval in intervals or self.intervals:
            df = await self.candle_source.fetch_ohlcv(symbol, interval, limit=self.bootstrap_limit)
            result[interval] = compute_snapshot(symbol, interval, df)
        return result
