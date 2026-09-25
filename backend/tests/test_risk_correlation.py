"""Unit tests for aegis.risk.correlation (Fase 17) - the previously-missing
PortfolioCorrelationEngine behind RiskEngine's CORRELATED_EXPOSURE check.
`compute_correlated_exposure` is pure (pandas Series in, no I/O); the
fetch/compute helpers are tested against fake repos/rest clients, no real
database or network.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from aegis.risk.correlation import (
    compute_account_correlated_exposure_from_candles,
    compute_account_correlated_exposure_from_rest,
    compute_correlated_exposure,
)


def _returns(values: list[float]) -> pd.Series:
    return pd.Series(values)


# -- compute_correlated_exposure (pure) --------------------------------------

def test_single_open_position_is_trivially_zero_correlated():
    result = compute_correlated_exposure({"BTCUSDT": _returns([0.01, -0.02, 0.03, 0.01, -0.01])})
    assert result.correlated_exposure_pct == 0.0
    assert result.correlated_pairs == []


def test_no_open_positions_is_zero_correlated():
    result = compute_correlated_exposure({})
    assert result.correlated_exposure_pct == 0.0


def test_two_perfectly_correlated_symbols_are_fully_correlated_exposure():
    values = [0.01, -0.02, 0.03, -0.01, 0.02, -0.015, 0.008]
    result = compute_correlated_exposure({"BTCUSDT": _returns(values), "ETHUSDT": _returns(values)})
    assert result.correlated_exposure_pct == pytest.approx(1.0)
    assert len(result.correlated_pairs) == 1
    assert result.correlated_pairs[0][:2] == ("BTCUSDT", "ETHUSDT")
    assert result.correlated_pairs[0][2] == pytest.approx(1.0)


def test_two_uncorrelated_symbols_have_zero_correlated_exposure():
    rng = np.random.default_rng(42)
    a = _returns(list(rng.normal(0, 0.01, 50)))
    b = _returns(list(rng.normal(0, 0.01, 50) * -1 + rng.normal(0, 0.05, 50)))
    # Deliberately unrelated series (independent random noise) - correlation
    # should land well below the default 0.70 threshold.
    result = compute_correlated_exposure({"BTCUSDT": a, "XRPUSDT": b})
    assert result.correlated_exposure_pct == 0.0


def test_partial_cluster_only_counts_symbols_in_a_correlated_pair():
    # BTC and ETH move together; SOL is independent noise - only BTC/ETH
    # should count toward correlated_exposure_pct, SOL should not.
    trend = [0.01, -0.02, 0.03, -0.01, 0.02, -0.015, 0.008, 0.011, -0.02, 0.005]
    rng = np.random.default_rng(7)
    independent = list(rng.normal(0, 0.03, len(trend)))
    result = compute_correlated_exposure({
        "BTCUSDT": _returns(trend), "ETHUSDT": _returns(trend), "SOLUSDT": _returns(independent),
    })
    assert result.correlated_exposure_pct == pytest.approx(2 / 3, abs=1e-3)
    assert len(result.correlated_pairs) == 1
    assert result.correlated_pairs[0][:2] == ("BTCUSDT", "ETHUSDT")


def test_correlation_below_threshold_does_not_count():
    # Same direction but weak relationship - correlation exists but under
    # the (raised) threshold, so it must not count as "correlated exposure".
    a = _returns([0.01, 0.02, -0.01, 0.015, -0.005, 0.01, -0.02, 0.03])
    b = _returns([0.02, -0.01, 0.03, -0.02, 0.01, -0.015, 0.005, -0.01])
    result = compute_correlated_exposure({"BTCUSDT": a, "ETHUSDT": b}, correlation_threshold=0.95)
    assert result.correlated_exposure_pct == 0.0


def test_insufficient_overlapping_history_is_not_treated_as_correlated():
    result = compute_correlated_exposure({"BTCUSDT": _returns([0.01, 0.02]), "ETHUSDT": _returns([0.01, 0.02])})
    assert result.correlated_exposure_pct == 0.0


# -- fetch/compute helpers (async, fake data sources) ------------------------

class _FakeCandleRepo:
    def __init__(self, closes_by_symbol: dict[str, list[float]]) -> None:
        self._closes = closes_by_symbol

    async def fetch_ohlcv(self, symbol, interval, limit=500, closed_only=True):
        closes = self._closes.get(symbol, [])
        return pd.DataFrame({"close": closes})


class _FakeKline:
    def __init__(self, close: float, close_time_ms: int) -> None:
        self.close = close
        self.close_time_ms = close_time_ms


class _FakeRestClient:
    def __init__(self, closes_by_symbol: dict[str, list[float]]) -> None:
        self._closes = closes_by_symbol

    async def get_klines(self, symbol, interval, limit=500):
        closes = self._closes.get(symbol, [])
        # Deliberately returned newest-first (like BingX) to prove the
        # helper re-sorts by close_time_ms rather than trusting call order.
        return [_FakeKline(c, i) for i, c in enumerate(closes)][::-1]


@pytest.mark.asyncio
async def test_compute_from_candles_returns_zero_with_fewer_than_two_symbols():
    repo = _FakeCandleRepo({"BTCUSDT": [100.0] * 20})
    result = await compute_account_correlated_exposure_from_candles(repo, ["BTCUSDT"], "1h")
    assert result.correlated_exposure_pct == 0.0


@pytest.mark.asyncio
async def test_compute_from_candles_detects_correlated_pair():
    closes = [100 + i + (1 if i % 2 == 0 else -1) for i in range(20)]
    repo = _FakeCandleRepo({"BTCUSDT": closes, "ETHUSDT": closes})
    result = await compute_account_correlated_exposure_from_candles(repo, ["BTCUSDT", "ETHUSDT"], "1h")
    assert result.correlated_exposure_pct == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_compute_from_rest_klines_handles_newest_first_ordering():
    closes = [100 + i + (1 if i % 2 == 0 else -1) for i in range(20)]
    rest = _FakeRestClient({"BTCUSDT": closes, "ETHUSDT": closes})
    result = await compute_account_correlated_exposure_from_rest(rest, ["BTCUSDT", "ETHUSDT"], "15m")
    assert result.correlated_exposure_pct == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_compute_from_rest_klines_returns_zero_with_fewer_than_two_symbols():
    rest = _FakeRestClient({"BTCUSDT": [100.0] * 20})
    result = await compute_account_correlated_exposure_from_rest(rest, ["BTCUSDT"], "15m")
    assert result.correlated_exposure_pct == 0.0
