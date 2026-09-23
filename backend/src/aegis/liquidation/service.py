"""LiquidationEngine - spec section 29.

Ingests the market-wide `!forceOrder@arstream` WebSocket stream (every
liquidation on Binance Futures, all symbols - filtered down to the
configured universe here), persists raw events, and periodically computes
a windowed snapshot: liquidation notional by side, imbalance, a z-score
against recent history, acceleration, and a composite `squeeze_score`.

`squeeze_score`'s weights (0.4/0.3/0.3 below) are a documented hypothesis,
not a validated model - spec section 54 is explicit that feature weights
must earn their place through backtesting, never be assumed. Nothing here
produces a trade decision; it is one more dimension for a future
ConfluenceEngine.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from datetime import datetime

import pandas as pd

from aegis import stats as analytics
from aegis.logging_utils import get_logger, log_event
from aegis.providers.binance.models import LiquidationEvent
from aegis.providers.binance.ws_client import BinanceFuturesWebSocketClient

_LOG = get_logger("liquidation.service")

MIN_POINTS_FOR_FULL_HISTORY = 20
LIQUIDATION_STREAM = "!forceOrder@arstream"


@dataclass(slots=True)
class LiquidationSnapshot:
    symbol: str
    period: str
    as_of: datetime | None
    data_points: int
    quality: str  # NO_DATA | PARTIAL_HISTORY | OK

    long_liquidation_notional: float | None = None
    short_liquidation_notional: float | None = None
    long_liquidation_count: int | None = None
    short_liquidation_count: int | None = None
    liquidation_imbalance: float | None = None  # -1 (all shorts) .. +1 (all longs)
    liquidation_notional_zscore: float | None = None
    liquidation_acceleration_pct: float | None = None
    squeeze_score: float | None = None  # 0-100 heuristic, see module docstring

    def to_features_dict(self) -> dict:
        d = asdict(self)
        for key in ("symbol", "period", "as_of", "data_points", "quality"):
            d.pop(key, None)
        return {k: v for k, v in d.items() if v is not None}


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _squeeze_score(
    notional_zscore: float | None, imbalance: float | None, acceleration_pct: float | None
) -> float | None:
    components: list[tuple[float, float]] = []  # (value, weight)
    if notional_zscore is not None:
        components.append((_clamp(notional_zscore, 0.0, 5.0) / 5.0 * 100, 0.4))
    if imbalance is not None:
        components.append((abs(imbalance) * 100, 0.3))
    if acceleration_pct is not None:
        components.append((_clamp(acceleration_pct, 0.0, 200.0) / 200.0 * 100, 0.3))
    if not components:
        return None
    total_weight = sum(w for _, w in components)
    return round(sum(v * w for v, w in components) / total_weight, 2)


def compute_snapshot(symbol: str, period: str, windowed_df: pd.DataFrame) -> LiquidationSnapshot:
    if windowed_df.empty:
        return LiquidationSnapshot(symbol=symbol, period=period, as_of=None, data_points=0, quality="NO_DATA")

    total_notional = windowed_df["long_notional"] + windowed_df["short_notional"]
    latest = windowed_df.iloc[-1]
    long_notional = float(latest["long_notional"])
    short_notional = float(latest["short_notional"])
    total = long_notional + short_notional

    imbalance = (long_notional - short_notional) / total if total > 0 else None
    notional_zscore = analytics.latest_zscore(total_notional, min_points=5)
    acceleration = analytics.acceleration(total_notional)
    quality = "OK" if len(windowed_df) >= MIN_POINTS_FOR_FULL_HISTORY else "PARTIAL_HISTORY"

    return LiquidationSnapshot(
        symbol=symbol,
        period=period,
        as_of=pd.Timestamp(latest["bucket_time"]).to_pydatetime(),
        data_points=len(windowed_df),
        quality=quality,
        long_liquidation_notional=long_notional,
        short_liquidation_notional=short_notional,
        long_liquidation_count=int(latest["long_count"]),
        short_liquidation_count=int(latest["short_count"]),
        liquidation_imbalance=imbalance,
        liquidation_notional_zscore=notional_zscore,
        liquidation_acceleration_pct=acceleration,
        squeeze_score=_squeeze_score(notional_zscore, imbalance, acceleration),
    )


def seconds_to_period_label(seconds: int) -> str:
    """300 -> "5m", 3600 -> "1h", 86400 -> "1d" - falls back to "<n>s"."""
    if seconds % 86400 == 0:
        return f"{seconds // 86400}d"
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


class LiquidationEngine:
    def __init__(self, repository, feature_repo, settings) -> None:
        self.repo = repository
        self.feature_repo = feature_repo
        self.settings = settings
        self._symbols = set(settings.symbols)
        self._buffer: list[LiquidationEvent] = []
        self._ws: BinanceFuturesWebSocketClient | None = None

    def on_liquidation_message(self, data: dict) -> None:
        if data.get("e") != "forceOrder":
            return
        try:
            event = LiquidationEvent.from_ws_payload(data)
        except (KeyError, ValueError, TypeError) as exc:
            log_event(_LOG, "liquidation_parse_error", level=40, error=str(exc))
            return
        if event.symbol not in self._symbols:
            return  # market-wide stream - keep only the configured universe
        self._buffer.append(event)

    async def flush_buffer(self) -> int:
        if not self._buffer:
            return 0
        batch, self._buffer = self._buffer, []
        await self.repo.insert_events(batch)
        return len(batch)

    async def compute_and_store_snapshot(self, symbol: str) -> LiquidationSnapshot:
        df = await self.repo.fetch_windowed_stats(
            symbol, self.settings.liquidation_window_seconds, self.settings.liquidation_baseline_buckets
        )
        period = seconds_to_period_label(self.settings.liquidation_window_seconds)
        snapshot = compute_snapshot(symbol, period, df)
        await self.feature_repo.upsert_snapshot(snapshot)
        return snapshot

    async def run_snapshot_cycle(self) -> dict[str, LiquidationSnapshot]:
        await self.flush_buffer()
        results: dict[str, LiquidationSnapshot] = {}
        for symbol in self.settings.symbols:
            try:
                results[symbol] = await self.compute_and_store_snapshot(symbol)
            except Exception as exc:  # noqa: BLE001 - one bad symbol must not skip the rest
                log_event(_LOG, "liquidation_snapshot_failed", level=40, symbol=symbol, error=str(exc))
        return results

    async def _snapshot_loop(self) -> None:
        while True:
            await asyncio.sleep(self.settings.liquidation_snapshot_interval_seconds)
            results = await self.run_snapshot_cycle()
            for symbol, snapshot in results.items():
                log_event(_LOG, "liquidation_snapshot", symbol=symbol, quality=snapshot.quality,
                           squeeze_score=snapshot.squeeze_score)

    async def run(self) -> None:
        self._ws = BinanceFuturesWebSocketClient(streams=[LIQUIDATION_STREAM], testnet=self.settings.binance_testnet)
        snapshot_task = asyncio.create_task(self._snapshot_loop())
        try:
            await self._ws.run(on_message=self.on_liquidation_message)
        finally:
            snapshot_task.cancel()

    def stop(self) -> None:
        if self._ws is not None:
            self._ws.stop()
