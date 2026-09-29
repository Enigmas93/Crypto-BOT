"""Fase 24 - trend engine: signal parity with the research code, paper
accounting, the daily loop and funding settlement."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from aegis.health.watchdog import HealthInputs, evaluate_health
from aegis.trend.book import Book, Position
from aegis.trend.engine import TrendPaperEngine
from aegis.trend.strategy import LOOKBACKS, required_bars, target_weights
from aegis.db.trend_repository import TrendState


def _research_weights(P: pd.DataFrame, lookbacks=LOOKBACKS, short_scale=0.5) -> pd.DataFrame:
    """Verbatim logic of the research script (trend_long.py) that produced the evidence."""
    sig = sum(np.sign(P.pct_change(L)) for L in lookbacks) / len(lookbacks)
    sig = sig.where(sig > 0, sig * short_scale)
    iv = 1 / P.pct_change().rolling(20).std()
    return (sig * iv).div(iv.sum(axis=1), axis=0)


def _walk(seed: int, n: int = 150) -> list[float]:
    rng = np.random.default_rng(seed)
    return list(100 * np.exp(np.cumsum(rng.normal(0.0, 0.03, n))))


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
def test_signal_matches_research_code(seed):
    closes = {"BTCUSDT": _walk(seed), "ETHUSDT": _walk(seed + 100)}
    P = pd.DataFrame(closes)
    expected = _research_weights(P)
    for t in (required_bars() - 1, 90, 149):
        got = target_weights({s: c[: t + 1] for s, c in closes.items()})
        for s in closes:
            assert got[s].weight == pytest.approx(expected[s].iloc[t], rel=1e-9, abs=1e-12)


def test_signal_votes_and_short_scaling():
    up = [100 + i for i in range(80)]
    down = [200 - i for i in range(80)]
    sig = target_weights({"BTCUSDT": up, "ETHUSDT": down})
    assert set(sig["BTCUSDT"].votes.values()) == {1} and sig["BTCUSDT"].signal == 1.0
    assert set(sig["ETHUSDT"].votes.values()) == {-1} and sig["ETHUSDT"].signal == -0.5
    assert sig["BTCUSDT"].weight > 0 > sig["ETHUSDT"].weight
    assert abs(sig["BTCUSDT"].weight) + abs(sig["ETHUSDT"].weight) <= 1.0 + 1e-12


def test_short_history_refuses_to_trade():
    with pytest.raises(ValueError):
        target_weights({"BTCUSDT": [1.0] * (required_bars() - 1)})


# -- paper book ----------------------------------------------------------------
def test_open_flip_and_close_accounting():
    b = Book(cash=1000.0)
    f1 = b.apply_fill("BTCUSDT", 0.01, 50_000, fee_rate=0.0005, slippage=0.0)
    assert f1.fee == pytest.approx(0.25) and b.cash == pytest.approx(999.75)
    assert b.positions["BTCUSDT"].entry_price == 50_000
    assert b.equity({"BTCUSDT": 51_000}) == pytest.approx(999.75 + 10.0)
    # flip long 0.01 -> short 0.01 at 51k: realize +10 on the long, new short entry 51k
    f2 = b.apply_fill("BTCUSDT", -0.02, 51_000, fee_rate=0.0005, slippage=0.0)
    assert f2.realized_pnl == pytest.approx(10.0)
    assert (f2.side_before, f2.side_after) == ("LONG", "SHORT")
    assert b.positions["BTCUSDT"].qty == pytest.approx(-0.01) and b.positions["BTCUSDT"].entry_price == 51_000
    f3 = b.apply_fill("BTCUSDT", 0.01, 50_000, fee_rate=0.0, slippage=0.0)
    assert f3.realized_pnl == pytest.approx(10.0) and f3.side_after == "FLAT"
    assert b.positions["BTCUSDT"].qty == 0.0


def test_slippage_always_hurts():
    b = Book(cash=1000.0)
    buy = b.apply_fill("ETHUSDT", 1.0, 100.0, fee_rate=0.0, slippage=0.001)
    sell = b.apply_fill("ETHUSDT", -1.0, 100.0, fee_rate=0.0, slippage=0.001)
    assert buy.price > 100 > sell.price and b.cash < 1000.0


def test_funding_long_pays_short_receives():
    b = Book(cash=1000.0, positions={"BTCUSDT": Position(0.01, 50_000), "ETHUSDT": Position(-1.0, 2_000)})
    assert b.apply_funding("BTCUSDT", 0.0001, 50_000) == pytest.approx(-0.05)
    assert b.apply_funding("ETHUSDT", 0.0001, 2_000) == pytest.approx(0.2)


def test_rebalance_skips_small_drift_but_not_side_changes():
    b = Book(cash=1000.0, positions={"BTCUSDT": Position(0.01, 50_000)})   # 50% of equity long
    assert b.rebalance({"BTCUSDT": 0.51}, {"BTCUSDT": 50_000}) == []        # 1% drift: skipped
    fills = b.rebalance({"BTCUSDT": -0.25}, {"BTCUSDT": 50_000})
    assert len(fills) == 1 and fills[0].side_after == "SHORT"


# -- engine loop -----------------------------------------------------------------
@dataclass
class _Bar:
    open_time_ms: int
    close_time_ms: int
    close: float


class _Market:
    def __init__(self, day0: datetime, closes: dict[str, list[float]]):
        self.day0, self.closes = day0, closes
        self.funding: dict[str, list[tuple[int, float, float]]] = {s: [] for s in closes}

    async def get_klines(self, symbol, interval, limit=500):
        bars = []
        for i, c in enumerate(self.closes[symbol]):
            o = int((self.day0 + timedelta(days=i)).timestamp() * 1000)
            bars.append(_Bar(o, o + 86_400_000 - 1, c))
        return bars[-limit:]

    async def get_funding_rate_history(self, symbol, start_ms=None, limit=100):
        return [e for e in self.funding[symbol] if start_ms is None or e[0] >= start_ms]


class _Repo:
    def __init__(self):
        self.state = None
        self.saves = []

    async def load(self, account_id, starting_equity, now_ms):
        if self.state is None:
            self.state = TrendState(Book(cash=starting_equity), None, now_ms)
        return self.state

    async def save(self, account_id, state, prices, now, fills=(), funding=(), signal=None):
        self.saves.append((now, list(fills), list(funding), signal))


def _setup():
    day0 = datetime(2026, 6, 1, tzinfo=UTC)
    n = 90   # bars day0..day0+89; the last one is "today" (still forming)
    market = _Market(day0, {"BTCUSDT": [100 + i for i in range(n)], "ETHUSDT": [50 + 0.5 * i for i in range(n)]})
    today = day0 + timedelta(days=n - 1)
    return market, _Repo(), today


def test_engine_rebalances_once_per_daily_bar_after_delay():
    market, repo, today = _setup()
    engine = TrendPaperEngine(market, repo)
    early = asyncio.run(engine.run_once(today + timedelta(minutes=2)))
    assert early["action"] == "HOLD"                     # inside REBALANCE_DELAY
    first = asyncio.run(engine.run_once(today + timedelta(minutes=6)))
    assert first["action"] == "REBALANCED"
    assert {f.side_after for f in first["fills"]} == {"LONG"}
    assert repo.state.last_rebalance_bar == today - timedelta(days=1)
    again = asyncio.run(engine.run_once(today + timedelta(hours=3)))
    assert again["action"] == "HOLD" and again["fills"] == []


def test_funding_waits_until_every_held_symbol_has_published():
    market, repo, today = _setup()
    engine = TrendPaperEngine(market, repo)
    asyncio.run(engine.run_once(today + timedelta(minutes=6)))          # opens both longs
    cash = repo.state.book.cash
    t8 = int((today + timedelta(hours=8)).timestamp() * 1000)
    market.funding["BTCUSDT"].append((t8, 0.0001, 190.0))
    r = asyncio.run(engine.run_once(today + timedelta(hours=8, minutes=1)))
    assert r["funding"] == [] and repo.state.book.cash == cash          # ETH not published yet
    market.funding["ETHUSDT"].append((t8, 0.0001, 95.0))
    r = asyncio.run(engine.run_once(today + timedelta(hours=8, minutes=2)))
    assert {s for s, _, _ in r["funding"]} == {"BTCUSDT", "ETHUSDT"}
    assert repo.state.book.cash < cash                                 # longs paid
    r = asyncio.run(engine.run_once(today + timedelta(hours=8, minutes=3)))
    assert r["funding"] == []                                          # never applied twice


# -- watchdog --------------------------------------------------------------------
def _health(now, updated, last_bar):
    return evaluate_health(HealthInputs(now=now, candle_latest_close={}, cursors={}, expected_fixed=[],
                                        momentum_accounts=[], trend_updated_at=updated, trend_last_bar=last_bar))


def test_watchdog_trend_rules():
    now = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
    yesterday = datetime(2026, 9, 28, tzinfo=UTC)
    assert _health(now, now - timedelta(minutes=1), yesterday) == []
    assert _health(now, None, None) == []                              # engine never deployed: silent
    assert [i.key for i in _health(now, now - timedelta(minutes=30), yesterday)] == ["engine:trend_paper"]
    stale_bar = _health(now, now, yesterday - timedelta(days=1))
    assert [i.key for i in stale_bar] == ["engine:trend_paper_rebalance"]
    just_after_close = datetime(2026, 9, 29, 0, 10, tzinfo=UTC)
    assert _health(just_after_close, just_after_close, yesterday - timedelta(days=1)) == []   # grace window
