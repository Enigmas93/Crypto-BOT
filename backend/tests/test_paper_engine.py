"""Tests PaperTradingEngine.run_once()'s branching logic against fake,
in-memory repos - the same DI pattern test_backtest_engine.py uses for
BacktestEngine, so the decision logic is verified without touching a real
database. PaperRepository itself is separately verified against real
Postgres in test_paper_repository_integration.py.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from aegis.execution.models import OpenPosition
from aegis.paper.engine import PaperTradingEngine
from aegis.paper.models import PaperTradingConfig
from aegis.providers.binance.models import SymbolRules
from aegis.risk.kill_switch import evaluate_kill_switch_triggers
from aegis.risk.service import AccountState
from aegis.strategy.strategies import STRATEGY_BREAKOUT


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
    rng = np.random.default_rng(seed)
    closes = np.empty(n)
    closes[:consolidation_len] = 100.0 + rng.normal(0, 0.5, consolidation_len)
    breakout_len = n - consolidation_len
    closes[consolidation_len:] = 100.0 + np.linspace(2, 20, breakout_len) + rng.normal(0, 0.3, breakout_len)
    volume = np.abs(np.full(n, 100.0) + rng.normal(0, 5, n))
    volume[consolidation_len:consolidation_len + 5] *= 4.0
    return closes, volume


class _FakeCandleRepo:
    def __init__(self, df: pd.DataFrame, df_by_symbol: dict[str, pd.DataFrame] | None = None):
        self._df = df
        self._df_by_symbol = df_by_symbol or {}

    async def fetch_ohlcv(self, symbol, interval, limit=500, closed_only=True):
        df = self._df_by_symbol.get(symbol, self._df)
        return df.tail(limit).reset_index(drop=True)


class _FakePaperRepo:
    def __init__(self):
        self.positions: dict[tuple[str, str], OpenPosition] = {}
        self.trades: list = []
        self.cursors: dict[tuple[str, str, str], datetime] = {}

    async def get_open_position(self, account_id, symbol):
        return self.positions.get((account_id, symbol))

    async def get_open_symbols(self, account_id):
        return [sym for (acct, sym) in self.positions if acct == account_id]

    async def open_position(self, account_id, symbol, position):
        if (account_id, symbol) in self.positions:
            raise ValueError("already open")
        self.positions[(account_id, symbol)] = position

    async def close_position(self, account_id, symbol):
        self.positions.pop((account_id, symbol), None)

    async def record_trade(self, trade):
        self.trades.append(trade)

    async def get_cursor(self, account_id, symbol, interval):
        return self.cursors.get((account_id, symbol, interval))

    async def set_cursor(self, account_id, symbol, interval, close_time):
        self.cursors[(account_id, symbol, interval)] = close_time


class _FakeRiskRepo:
    def __init__(self, account: AccountState):
        self.account = account
        self.outcomes: list[float] = []
        self.risk_events: list[tuple] = []
        self.exposure_calls: list[tuple] = []

    async def get_account_state(self, account_id):
        return self.account

    async def insert_risk_event(self, account_id, symbol, side, decision, strategy_id=None):
        self.risk_events.append((account_id, symbol, side, decision))

    async def set_exposure(self, account_id, open_positions_count, correlated_exposure_pct=0.0):
        self.exposure_calls.append((account_id, open_positions_count, correlated_exposure_pct))

    async def record_trade_outcome(self, account_id, pnl):
        self.outcomes.append(pnl)
        new_equity = self.account.equity + pnl
        self.account = AccountState(
            equity=new_equity, peak_equity=max(self.account.peak_equity, new_equity),
            daily_starting_equity=self.account.daily_starting_equity,
            daily_realized_pnl=self.account.daily_realized_pnl + pnl,
            consecutive_losses=(self.account.consecutive_losses + 1) if pnl < 0 else 0,
            open_positions_count=0, correlated_exposure_pct=self.account.correlated_exposure_pct,
        )
        return self.account


class _KillSwitchState:
    def __init__(self, is_triggered=False, reasons=None):
        self.is_triggered = is_triggered
        self.reasons = reasons


class _FakeKillSwitchRepo:
    def __init__(self, pre_triggered: bool = False):
        self.state = _KillSwitchState(is_triggered=pre_triggered, reasons=["PRE_SET"] if pre_triggered else None)

    async def get_state(self, account_id):
        return self.state

    async def check_and_maybe_trigger(self, account_id, equity, peak_equity, consecutive_losses, settings):
        if self.state.is_triggered:
            return self.state
        reasons = evaluate_kill_switch_triggers(
            equity, peak_equity, consecutive_losses, settings.max_drawdown, settings.loss_streak_halt_threshold,
        )
        if reasons:
            self.state = _KillSwitchState(is_triggered=True, reasons=reasons)
        return self.state


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


def _account(equity=1000.0) -> AccountState:
    return AccountState(equity=equity, peak_equity=equity, daily_starting_equity=equity,
                         daily_realized_pnl=0.0, consecutive_losses=0, open_positions_count=0)


def _engine(df, account=None, kill_switch_triggered=False):
    candle_repo = _FakeCandleRepo(df)
    paper_repo = _FakePaperRepo()
    risk_repo = _FakeRiskRepo(account or _account())
    kill_switch_repo = _FakeKillSwitchRepo(pre_triggered=kill_switch_triggered)
    engine = PaperTradingEngine(candle_repo, paper_repo, risk_repo, kill_switch_repo, _Settings())
    return engine, paper_repo, risk_repo, kill_switch_repo


@pytest.mark.asyncio
async def test_not_enough_history():
    closes, volume = _consolidation_then_breakout(n=50, consolidation_len=40)
    df = _candles_df(closes, volume)
    engine, *_ = _engine(df)
    config = PaperTradingConfig(symbol="BTCUSDT", interval="1h", warmup_bars=210)

    result = await engine.run_once(config, _rules())
    assert result["action"] == "NOT_ENOUGH_HISTORY"


@pytest.mark.asyncio
async def test_raises_when_account_not_initialized():
    closes, volume = _consolidation_then_breakout()
    df = _candles_df(closes, volume)
    candle_repo = _FakeCandleRepo(df)
    paper_repo = _FakePaperRepo()

    class _NoAccountRiskRepo:
        async def get_account_state(self, account_id):
            return None

    kill_switch_repo = _FakeKillSwitchRepo()
    engine = PaperTradingEngine(candle_repo, paper_repo, _NoAccountRiskRepo(), kill_switch_repo, _Settings())
    config = PaperTradingConfig(symbol="BTCUSDT", interval="1h", strategy_ids=(STRATEGY_BREAKOUT,), warmup_bars=210)

    with pytest.raises(ValueError):
        await engine.run_once(config, _rules())


@pytest.mark.asyncio
async def test_no_new_candle_when_cursor_already_at_latest_close():
    closes, volume = _consolidation_then_breakout()
    df = _candles_df(closes, volume)
    engine, paper_repo, *_ = _engine(df)
    config = PaperTradingConfig(symbol="BTCUSDT", interval="1h", strategy_ids=(STRATEGY_BREAKOUT,), warmup_bars=210)
    paper_repo.cursors[(config.account_id, config.symbol, config.interval)] = df["close_time"].iloc[-1]

    result = await engine.run_once(config, _rules())
    assert result["action"] == "NO_NEW_CANDLE"


@pytest.mark.asyncio
async def test_position_still_open_when_neither_stop_nor_tp_hit():
    closes, volume = _consolidation_then_breakout()
    df = _candles_df(closes, volume)
    engine, paper_repo, *_ = _engine(df)
    config = PaperTradingConfig(symbol="BTCUSDT", interval="1h")
    last_close = float(df["close"].iloc[-1])
    paper_repo.positions[(config.account_id, config.symbol)] = OpenPosition(
        side="LONG", entry_time=df["open_time"].iloc[-5], entry_price=last_close - 50.0,
        stop_price=last_close - 100.0, take_profit_price=last_close + 100.0,
        quantity=0.01, risk_amount=1.0, confluence_score=40.0, reasons=["test"],
    )

    result = await engine.run_once(config, _rules())
    assert result["action"] == "POSITION_STILL_OPEN"
    assert (config.account_id, config.symbol) in paper_repo.positions
    assert paper_repo.trades == []


@pytest.mark.asyncio
async def test_position_closed_on_stop_hit_records_trade_and_updates_account():
    closes, volume = _consolidation_then_breakout()
    df = _candles_df(closes, volume)
    engine, paper_repo, risk_repo, kill_switch_repo = _engine(df)
    config = PaperTradingConfig(symbol="BTCUSDT", interval="1h")
    last_bar = df.iloc[-1]
    # stop sits just above the bar's high for a SHORT -> guaranteed hit
    paper_repo.positions[(config.account_id, config.symbol)] = OpenPosition(
        side="SHORT", entry_time=df["open_time"].iloc[-5], entry_price=float(last_bar["close"]),
        stop_price=float(last_bar["high"]) - 0.01, take_profit_price=float(last_bar["close"]) - 50.0,
        quantity=0.01, risk_amount=1.0, confluence_score=40.0, reasons=["test"],
    )

    result = await engine.run_once(config, _rules())

    assert result["action"] == "POSITION_CLOSED"
    assert result["trade"].exit_reason == "STOP"
    assert result["trade"].net_pnl < 0
    assert (config.account_id, config.symbol) not in paper_repo.positions
    assert len(paper_repo.trades) == 1
    assert risk_repo.outcomes == [pytest.approx(result["trade"].net_pnl)]


@pytest.mark.asyncio
async def test_position_closed_computes_correlated_exposure_across_remaining_open_symbols():
    # Regression (Fase 17 - PortfolioCorrelationEngine): closing BTCUSDT
    # while two other symbols stay open, all with PERFECTLY correlated
    # candles (identical closes), must report correlated_exposure_pct as
    # 1.0 for the two remaining positions - not the 0.0 default this always
    # silently reported before.
    closes, volume = _consolidation_then_breakout()
    df = _candles_df(closes, volume)
    candle_repo = _FakeCandleRepo(df, df_by_symbol={"BTCUSDT": df, "ETHUSDT": df, "SOLUSDT": df})
    paper_repo = _FakePaperRepo()
    risk_repo = _FakeRiskRepo(_account())
    engine = PaperTradingEngine(candle_repo, paper_repo, risk_repo, _FakeKillSwitchRepo(), _Settings())
    config = PaperTradingConfig(symbol="BTCUSDT", interval="1h")
    last_bar = df.iloc[-1]
    for symbol in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):
        paper_repo.positions[(config.account_id, symbol)] = OpenPosition(
            side="SHORT", entry_time=df["open_time"].iloc[-5], entry_price=float(last_bar["close"]),
            stop_price=float(last_bar["high"]) - 0.01, take_profit_price=float(last_bar["close"]) - 50.0,
            quantity=0.01, risk_amount=1.0, confluence_score=40.0, reasons=["test"],
        )

    result = await engine.run_once(config, _rules())

    assert result["action"] == "POSITION_CLOSED"
    assert risk_repo.exposure_calls[-1] == (config.account_id, 2, pytest.approx(1.0))


@pytest.mark.asyncio
async def test_kill_switch_blocks_new_entry_without_opening_a_position():
    closes, volume = _consolidation_then_breakout()
    df = _candles_df(closes, volume)
    engine, paper_repo, *_ = _engine(df, kill_switch_triggered=True)
    config = PaperTradingConfig(symbol="BTCUSDT", interval="1h", strategy_ids=(STRATEGY_BREAKOUT,), warmup_bars=210)

    result = await engine.run_once(config, _rules())

    assert result["action"] == "KILL_SWITCH_BLOCKED"
    assert paper_repo.positions == {}
    # cursor still advances even when blocked - a blocked cycle isn't re-processed forever
    assert paper_repo.cursors[(config.account_id, config.symbol, config.interval)] == df["close_time"].iloc[-1]


@pytest.mark.asyncio
async def test_no_signal_during_flat_consolidation():
    closes, volume = _consolidation_then_breakout(n=280, consolidation_len=280)  # never breaks out
    df = _candles_df(closes, volume)
    engine, paper_repo, *_ = _engine(df)
    config = PaperTradingConfig(symbol="BTCUSDT", interval="1h", strategy_ids=(STRATEGY_BREAKOUT,), warmup_bars=210)

    result = await engine.run_once(config, _rules())
    assert result["action"] in ("NO_SIGNAL", "NO_STOP_AVAILABLE")
    assert paper_repo.positions == {}


@pytest.mark.asyncio
async def test_entry_blocked_by_risk_engine_with_tiny_equity():
    closes, volume = _consolidation_then_breakout()
    df = _candles_df(closes, volume)
    engine, paper_repo, risk_repo, *_ = _engine(df, account=_account(equity=0.01))
    config = PaperTradingConfig(symbol="BTCUSDT", interval="1h", strategy_ids=(STRATEGY_BREAKOUT,), warmup_bars=210)

    result = await engine.run_once(config, _rules())

    if result["action"] == "ENTRY_BLOCKED":
        assert paper_repo.positions == {}
        assert len(risk_repo.risk_events) == 1
        assert risk_repo.risk_events[0][3].decision != "PASS"
    else:
        # the breakout may not fire on this exact bar - either way, tiny equity must never open a position
        assert result["action"] in ("NO_SIGNAL", "NO_STOP_AVAILABLE", "ENTRY_BLOCKED")
        assert paper_repo.positions == {}


@pytest.mark.asyncio
async def test_entry_opened_persists_a_position_with_positive_quantity():
    # unlike a backtest (which scans every bar), run_once only ever looks
    # at the LATEST bar - so the dataset must end exactly on the breakout
    # bar itself, not just contain one somewhere in its history
    closes, volume = _consolidation_then_breakout(n=261, consolidation_len=260)
    df = _candles_df(closes, volume)
    engine, paper_repo, *_ = _engine(df)
    config = PaperTradingConfig(symbol="BTCUSDT", interval="1h", strategy_ids=(STRATEGY_BREAKOUT,), warmup_bars=210)

    result = await engine.run_once(config, _rules())

    assert result["action"] == "ENTRY_OPENED"
    assert result["quantity"] > 0
    position = paper_repo.positions[(config.account_id, config.symbol)]
    assert position.side == result["side"]
    assert position.quantity == pytest.approx(result["quantity"])
