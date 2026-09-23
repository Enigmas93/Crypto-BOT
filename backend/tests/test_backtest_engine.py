from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from aegis.backtest.engine import BacktestEngine
from aegis.backtest.models import BacktestConfig
from aegis.news.conflict import AssetNewsStatus
from aegis.providers.binance.models import SymbolRules
from aegis.strategy.strategies import STRATEGY_BREAKOUT, STRATEGY_EVENT_REACTION


def _rules() -> SymbolRules:
    return SymbolRules(symbol="BTCUSDT", status="TRADING", price_precision=2, quantity_precision=5,
                        tick_size=0.1, step_size=0.00001, min_notional=5.0)


def _candles_df(closes: np.ndarray, volume: np.ndarray,
                 start: datetime = datetime(2026, 1, 1, tzinfo=UTC)) -> pd.DataFrame:
    n = len(closes)
    open_time = pd.date_range(start, periods=n, freq="1h")
    highs = closes + 0.3
    lows = closes - 0.3
    opens = np.concatenate([[closes[0]], closes[:-1]])
    return pd.DataFrame({
        "open_time": open_time,
        "close_time": open_time + pd.Timedelta(hours=1) - pd.Timedelta(milliseconds=1),
        "open": opens, "high": highs, "low": lows, "close": closes,
        "volume": volume, "quote_volume": closes * volume,
        "trades": np.full(n, 50), "taker_buy_base_volume": volume * 0.5,
        "taker_buy_quote_volume": closes * volume * 0.5, "is_closed": True,
    })


def _consolidation_then_breakout(n: int = 320, consolidation_len: int = 260, seed: int = 7):
    """A flat, choppy range (real but bounded volume/price variance - not
    a suspiciously noiseless line) followed by a sharp breakout with a
    genuine volume spike. Chosen empirically to reliably fire
    STRATEGY_BREAKOUT - `TechnicalSnapshot`'s indicators need real bar-to-
    bar variance to mean anything (constant volume gives a zero-variance
    z-score, i.e. undefined, not zero)."""
    rng = np.random.default_rng(seed)
    closes = np.empty(n)
    closes[:consolidation_len] = 100.0 + rng.normal(0, 0.5, consolidation_len)
    breakout_len = n - consolidation_len
    closes[consolidation_len:] = 100.0 + np.linspace(2, 20, breakout_len) + rng.normal(0, 0.3, breakout_len)

    volume = np.abs(np.full(n, 100.0) + rng.normal(0, 5, n))
    volume[consolidation_len:consolidation_len + 5] *= 4.0  # the breakout's volume spike
    return closes, volume


class _FakeCandleRepo:
    def __init__(self, df: pd.DataFrame):
        self._df = df

    async def fetch_ohlcv(self, symbol, interval, limit=500, closed_only=True):
        return self._df.tail(limit).reset_index(drop=True)


class _Settings:
    risk_per_trade = 0.01
    max_daily_loss = 0.10
    max_drawdown = 0.50
    drawdown_caution_pct = 0.20
    drawdown_reduced_risk_pct = 0.30
    risk_reduction_factor_caution = 0.75
    risk_reduction_factor_reduced = 0.50
    risk_reduction_factor_loss_streak = 0.50
    loss_streak_cooldown_threshold = 3
    loss_streak_reduce_risk_threshold = 5
    loss_streak_halt_threshold = 20
    max_leverage = 5
    min_r_multiple = 0.1
    max_open_positions = 10
    max_correlated_exposure_pct = 0.90
    maintenance_margin_rate_estimate = 0.004
    liquidation_safety_margin = 0.75


@pytest.mark.asyncio
async def test_raises_when_not_enough_history():
    closes, volume = _consolidation_then_breakout(n=50, consolidation_len=40)
    df = _candles_df(closes, volume)
    engine = BacktestEngine(_FakeCandleRepo(df), _Settings())
    config = BacktestConfig(symbol="BTCUSDT", interval="1h", warmup_bars=210)
    with pytest.raises(ValueError):
        await engine.run(config, _rules())


@pytest.mark.asyncio
async def test_end_to_end_run_on_a_consolidation_breakout_opens_and_closes_trades():
    closes, volume = _consolidation_then_breakout()
    df = _candles_df(closes, volume)
    engine = BacktestEngine(_FakeCandleRepo(df), _Settings())
    config = BacktestConfig(symbol="BTCUSDT", interval="1h", initial_equity=1000.0,
                             strategy_ids=(STRATEGY_BREAKOUT,), warmup_bars=210)

    result = await engine.run(config, _rules())

    assert len(result.trades) > 0, "a designed consolidation->breakout should trigger at least one trade"
    for trade in result.trades:
        assert trade.entry_time < trade.exit_time or trade.exit_reason == "END_OF_DATA"
        assert trade.quantity > 0
        assert trade.fees_paid > 0
        assert trade.side in ("LONG", "SHORT")
    assert len(result.equity_curve) == len(df) - config.warmup_bars
    assert result.metrics.total_trades == len(result.trades)


@pytest.mark.asyncio
async def test_tiny_equity_is_blocked_by_the_risk_engine_not_silently_ignored():
    closes, volume = _consolidation_then_breakout()
    df = _candles_df(closes, volume)
    engine = BacktestEngine(_FakeCandleRepo(df), _Settings())
    config = BacktestConfig(symbol="BTCUSDT", interval="1h", initial_equity=0.01,
                             strategy_ids=(STRATEGY_BREAKOUT,), warmup_bars=210)

    result = await engine.run(config, _rules())

    # equity so small that risk_amount*quantity can never clear min_notional -> RiskEngine blocks every proposal
    assert result.trades == []
    assert result.metrics.final_equity == pytest.approx(0.01)


@pytest.mark.asyncio
async def test_no_lookahead_a_future_price_shock_never_changes_earlier_decisions():
    """The core correctness property (spec section 69): whatever happens
    after bar i must have zero effect on the decision made *at* bar i.
    Two datasets share an identical prefix through the breakout itself;
    only bars near the very end differ (a violent extra shock). Every
    trade whose entry falls inside the shared prefix must come out
    byte-identical between the two runs - if the shock leaked backward
    into indicator computation, it wouldn't."""
    base_closes, base_volume = _consolidation_then_breakout(n=320)
    shared_prefix_bars = 300

    shocked_closes = base_closes.copy()
    shocked_closes[shared_prefix_bars:] = shocked_closes[shared_prefix_bars] * 3.0
    shocked_volume = base_volume.copy()
    shocked_volume[shared_prefix_bars:] *= 10.0

    df_a = _candles_df(base_closes, base_volume)
    df_b = _candles_df(shocked_closes, shocked_volume)

    config = BacktestConfig(symbol="BTCUSDT", interval="1h", initial_equity=1000.0,
                             strategy_ids=(STRATEGY_BREAKOUT,), warmup_bars=210)

    result_a = await BacktestEngine(_FakeCandleRepo(df_a), _Settings()).run(config, _rules())
    result_b = await BacktestEngine(_FakeCandleRepo(df_b), _Settings()).run(config, _rules())

    prefix_cutoff = df_a["open_time"].iloc[shared_prefix_bars]
    # A trade that *enters* before the cutoff but exits after it is exposed
    # to bars that genuinely differ between A and B once it's open - that's
    # real, expected exposure, not lookahead. Only trades whose entire
    # lifecycle (entry AND exit) sits inside the untouched region prove the
    # no-lookahead property; a trade straddling the boundary proves nothing
    # either way and would make this test flaky by construction.
    trades_a = [t for t in result_a.trades if t.entry_time < prefix_cutoff and t.exit_time < prefix_cutoff]
    trades_b = [t for t in result_b.trades if t.entry_time < prefix_cutoff and t.exit_time < prefix_cutoff]

    assert len(trades_a) > 0, "test needs at least one trade inside the shared prefix to be meaningful"
    assert len(trades_a) == len(trades_b)
    for ta, tb in zip(trades_a, trades_b):
        assert ta.entry_time == tb.entry_time
        assert ta.entry_price == pytest.approx(tb.entry_price)
        assert ta.exit_time == tb.exit_time
        assert ta.exit_price == pytest.approx(tb.exit_price)
        assert ta.net_pnl == pytest.approx(tb.net_pnl)

    curve_a = [(t, e) for t, e in result_a.equity_curve if t < prefix_cutoff]
    curve_b = [(t, e) for t, e in result_b.equity_curve if t < prefix_cutoff]
    assert len(curve_a) == len(curve_b)
    for (ta, ea), (tb, eb) in zip(curve_a, curve_b):
        assert ta == tb
        assert ea == pytest.approx(eb)


# -- EVENT_REACTION wiring: news status must never leak from the future -----

class _FakeNewsRepo:
    def __init__(self, history):
        self._history = history
        self.calls = []

    async def fetch_status_history(self, asset, start, end):
        self.calls.append((asset, start, end))
        return self._history


@pytest.mark.asyncio
async def test_event_reaction_requested_without_news_repo_raises():
    closes, volume = _consolidation_then_breakout()
    df = _candles_df(closes, volume)
    engine = BacktestEngine(_FakeCandleRepo(df), _Settings())  # no news_repo
    config = BacktestConfig(symbol="BTCUSDT", interval="1h",
                             strategy_ids=(STRATEGY_EVENT_REACTION,), warmup_bars=210)
    with pytest.raises(ValueError):
        await engine.run(config, _rules())


@pytest.mark.asyncio
async def test_event_reaction_never_sees_a_news_status_computed_after_its_own_bar(monkeypatch):
    """The same no-lookahead property as the price test above, for news:
    a status change planted at the midpoint of the backtest's bar range
    must be invisible to every bar before it, and visible to every bar at
    or after it - never the other way, regardless of what
    fetch_status_history happens to return for the whole window."""
    closes, volume = _consolidation_then_breakout()
    df = _candles_df(closes, volume)
    warmup_bars = 210

    # Midpoint of the range the engine actually EVALUATES (bars
    # warmup_bars..len(df)-1), not of the whole array - a midpoint inside
    # the warmup region would never be observed at all, making the test
    # vacuously pass without proving anything.
    midpoint_time = df["close_time"].iloc[(warmup_bars + len(df)) // 2]
    before_status = AssetNewsStatus(asset="BTC", status="CONFIRMED", distinct_sources=2,
                                     item_count=2, dominant_sentiment="neutral")
    after_status = AssetNewsStatus(asset="BTC", status="CONFIRMED", distinct_sources=5,
                                    item_count=10, dominant_sentiment="positive")
    history = [
        (df["close_time"].iloc[0] - timedelta(hours=1), before_status),
        (midpoint_time, after_status),
    ]
    news_repo = _FakeNewsRepo(history)

    engine = BacktestEngine(_FakeCandleRepo(df), _Settings(), news_repo=news_repo)
    config = BacktestConfig(symbol="BTCUSDT", interval="1h",
                             strategy_ids=(STRATEGY_EVENT_REACTION,), warmup_bars=warmup_bars)

    import aegis.backtest.engine as engine_module
    real_evaluate_all = engine_module.evaluate_all
    observed: list[tuple] = []

    def spy(snapshot, strategy_ids, news_status=None):
        observed.append((snapshot.as_of, news_status))
        return real_evaluate_all(snapshot, strategy_ids, news_status=news_status)

    monkeypatch.setattr(engine_module, "evaluate_all", spy)

    await engine.run(config, _rules())

    assert news_repo.calls == [("BTC", df["open_time"].iloc[0], df["close_time"].iloc[-1])]
    assert len(observed) > 0
    # most_recent_status_as_of is inclusive of the exact cutoff instant (a
    # status computed AT this bar's own close time is legitimately visible
    # to it - only something computed strictly AFTER would be a leak), so
    # the bar sitting exactly at midpoint_time belongs in the "after" bucket.
    saw_before, saw_after = False, False
    for as_of, news_status in observed:
        if as_of < midpoint_time:
            assert news_status is None or news_status.dominant_sentiment == "neutral", (
                f"bar at {as_of} (< cutoff {midpoint_time}) leaked the future status: {news_status}"
            )
            if news_status is not None:
                saw_before = True
        else:
            assert news_status is not None and news_status.dominant_sentiment == "positive", (
                f"bar at {as_of} (>= cutoff {midpoint_time}) should see the updated status, got: {news_status}"
            )
            saw_after = True
    assert saw_before and saw_after, "test needs bars on both sides of the status change to be meaningful"
