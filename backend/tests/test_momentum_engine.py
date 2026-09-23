"""Tests MomentumTradingEngine.run_once_for_symbol()'s branching logic
against fake, in-memory repos and a fake REST/execution provider - same DI
pattern as test_shadow_engine.py. BinanceExecutionProvider itself is
separately verified in test_binance_execution_provider.py; klines_to_
closed_dataframe in test_momentum_candles.py; MomentumRepository against
real Postgres in test_momentum_repository_integration.py.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from aegis.execution.binance_provider import BracketOpenError, TrailingBracketOrders
from aegis.momentum.engine import MomentumTradingEngine
from aegis.momentum.models import MomentumConfig, MomentumPosition
from aegis.providers.binance.models import AlgoOrderResult, Kline, OrderResult, PositionRisk, SymbolRules
from aegis.risk.kill_switch import evaluate_kill_switch_triggers
from aegis.risk.service import AccountState
from aegis.strategy.strategies import STRATEGY_BREAKOUT


def _rules(symbol="ETHUSDT") -> SymbolRules:
    return SymbolRules(symbol=symbol, status="TRADING", price_precision=2, quantity_precision=3,
                        tick_size=0.01, step_size=0.001, min_notional=5.0)


def _consolidation_then_breakout(n: int = 261, consolidation_len: int = 260, seed: int = 7):
    rng = np.random.default_rng(seed)
    closes = np.empty(n)
    closes[:consolidation_len] = 100.0 + rng.normal(0, 0.5, consolidation_len)
    breakout_len = n - consolidation_len
    closes[consolidation_len:] = 100.0 + np.linspace(2, 20, breakout_len) + rng.normal(0, 0.3, breakout_len)
    volume = np.abs(np.full(n, 100.0) + rng.normal(0, 5, n))
    volume[consolidation_len:consolidation_len + 5] *= 4.0
    return closes, volume


def _klines(closes: np.ndarray, volume: np.ndarray, symbol="ETHUSDT",
            start: datetime = datetime(2026, 1, 1, tzinfo=UTC), interval_minutes: int = 60) -> list[Kline]:
    klines = []
    for i, (c, v) in enumerate(zip(closes, volume)):
        open_time = start + timedelta(minutes=interval_minutes * i)
        close_time = open_time + timedelta(minutes=interval_minutes) - timedelta(milliseconds=1)
        prev_close = closes[i - 1] if i > 0 else c
        klines.append(Kline(
            symbol=symbol, interval="1h", open_time_ms=int(open_time.timestamp() * 1000),
            close_time_ms=int(close_time.timestamp() * 1000), open=prev_close, high=c + 0.3, low=c - 0.3,
            close=c, volume=v, quote_volume=c * v, trades=50, taker_buy_base_volume=v * 0.5,
            taker_buy_quote_volume=c * v * 0.5, is_closed=True,
        ))
    return klines


class _FakeRestClient:
    def __init__(self, klines: list[Kline]):
        self._klines = klines

    async def get_klines(self, symbol, interval, limit=500):
        return self._klines[-limit:]


class _FakeMomentumRepo:
    def __init__(self):
        self.positions: dict[tuple[str, str], MomentumPosition] = {}
        self.trades: list = []
        self.cursors: dict[tuple[str, str, str], datetime] = {}

    async def get_open_position(self, account_id, symbol):
        return self.positions.get((account_id, symbol))

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

    async def get_account_state(self, account_id):
        return self.account

    async def insert_risk_event(self, account_id, symbol, side, decision, strategy_id=None):
        self.risk_events.append((account_id, symbol, side, decision))

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


class _FakeExecutionProvider:
    def __init__(self):
        self.position_amt = 0.0
        self.order_statuses: dict[int, AlgoOrderResult] = {}
        self.cancelled: list[int] = []
        self.bracket_error: Exception | None = None
        self.open_trailing_calls: list[tuple] = []
        self._next_id = 100

    def set_order_status(self, order_id: int, status: str, avg_price: float = 0.0):
        self.order_statuses[order_id] = AlgoOrderResult(
            order_id=order_id, client_order_id=f"aegis-{order_id}", symbol="ETHUSDT", side="SELL",
            type="STOP_MARKET", status=status, quantity=0.01, executed_qty=0.01 if status == "FILLED" else 0.0,
            avg_price=avg_price, reduce_only=True, close_position=False, stop_price=None,
            update_time_ms=1700000000000,
        )

    async def get_position(self, symbol):
        return PositionRisk(symbol=symbol, position_amt=self.position_amt, entry_price=0.0, mark_price=0.0,
                             unrealized_pnl=0.0, leverage=3, liquidation_price=0.0)

    async def get_order_status(self, symbol, order_id):
        return self.order_statuses.get(order_id) or AlgoOrderResult(
            order_id=order_id, client_order_id="x", symbol=symbol, side="SELL", type="STOP_MARKET",
            status="NEW", quantity=0.01, executed_qty=0.0, avg_price=0.0, reduce_only=True,
            close_position=False, stop_price=None, update_time_ms=1700000000000,
        )

    async def cancel_leftover_order(self, symbol, order_id):
        self.cancelled.append(order_id)

    async def open_trailing_bracket_position(self, symbol, side, quantity, stop_price, callback_rate_pct, leverage,
                                              activation_price=None):
        self.open_trailing_calls.append(
            (symbol, side, quantity, stop_price, callback_rate_pct, leverage, activation_price)
        )
        if self.bracket_error is not None:
            raise self.bracket_error
        self._next_id += 1
        entry_id, stop_id, trailing_id = self._next_id, self._next_id + 1, self._next_id + 2
        self._next_id += 2
        entry = OrderResult(order_id=entry_id, client_order_id="e", symbol=symbol,
                             side="BUY" if side == "LONG" else "SELL", type="MARKET", status="FILLED",
                             quantity=quantity, executed_qty=quantity, avg_price=stop_price,
                             reduce_only=False, close_position=False, stop_price=None, update_time_ms=1700000000000)
        stop = AlgoOrderResult(order_id=stop_id, client_order_id="s", symbol=symbol,
                                side="SELL" if side == "LONG" else "BUY", type="STOP_MARKET", status="NEW",
                                quantity=quantity, executed_qty=0.0, avg_price=0.0, reduce_only=True,
                                close_position=False, stop_price=stop_price, update_time_ms=1700000000000)
        trailing = AlgoOrderResult(order_id=trailing_id, client_order_id="t", symbol=symbol,
                                    side="SELL" if side == "LONG" else "BUY", type="TRAILING_STOP_MARKET",
                                    status="NEW", quantity=quantity, executed_qty=0.0, avg_price=0.0,
                                    reduce_only=True, close_position=False, stop_price=None,
                                    update_time_ms=1700000000000)
        return TrailingBracketOrders(entry=entry, stop=stop, trailing_stop=trailing)


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


def _engine(klines, account=None, kill_switch_triggered=False):
    rest = _FakeRestClient(klines)
    momentum_repo = _FakeMomentumRepo()
    risk_repo = _FakeRiskRepo(account or _account())
    kill_switch_repo = _FakeKillSwitchRepo(pre_triggered=kill_switch_triggered)
    execution = _FakeExecutionProvider()
    engine = MomentumTradingEngine(rest, momentum_repo, risk_repo, kill_switch_repo, execution, _Settings())
    return engine, momentum_repo, risk_repo, kill_switch_repo, execution


@pytest.mark.asyncio
async def test_raises_when_account_not_initialized():
    closes, volume = _consolidation_then_breakout()
    klines = _klines(closes, volume)

    class _NoAccountRiskRepo:
        async def get_account_state(self, account_id):
            return None

    engine = MomentumTradingEngine(_FakeRestClient(klines), _FakeMomentumRepo(), _NoAccountRiskRepo(),
                                    _FakeKillSwitchRepo(), _FakeExecutionProvider(), _Settings())
    config = MomentumConfig(strategy_ids=(STRATEGY_BREAKOUT,), warmup_bars=210)
    with pytest.raises(ValueError):
        await engine.run_once_for_symbol(config, "ETHUSDT", _rules(), momentum_score=12.0)


@pytest.mark.asyncio
async def test_not_enough_history():
    closes, volume = _consolidation_then_breakout(n=50, consolidation_len=40)
    klines = _klines(closes, volume)
    engine, *_ = _engine(klines)
    config = MomentumConfig(warmup_bars=210)
    result = await engine.run_once_for_symbol(config, "ETHUSDT", _rules(), momentum_score=12.0)
    assert result["action"] == "NOT_ENOUGH_HISTORY"


@pytest.mark.asyncio
async def test_no_new_candle_when_cursor_already_at_latest_close():
    closes, volume = _consolidation_then_breakout()
    klines = _klines(closes, volume)
    engine, momentum_repo, *_ = _engine(klines)
    config = MomentumConfig(strategy_ids=(STRATEGY_BREAKOUT,), warmup_bars=210)
    last_close_time = datetime.fromtimestamp(klines[-1].close_time_ms / 1000, tz=UTC)
    momentum_repo.cursors[(config.account_id, "ETHUSDT", config.interval)] = last_close_time

    result = await engine.run_once_for_symbol(config, "ETHUSDT", _rules(), momentum_score=12.0)
    assert result["action"] == "NO_NEW_CANDLE"


@pytest.mark.asyncio
async def test_position_still_open_when_exchange_reports_nonzero_amount():
    closes, volume = _consolidation_then_breakout()
    klines = _klines(closes, volume)
    engine, momentum_repo, _, _, execution = _engine(klines)
    config = MomentumConfig()
    momentum_repo.positions[(config.account_id, "ETHUSDT")] = MomentumPosition(
        side="LONG", entry_time=datetime(2026, 1, 1, tzinfo=UTC), entry_price=100.0, quantity=0.01,
        stop_order_id=101, trailing_order_id=102, stop_price=96.0, risk_amount=1.0,
        confluence_score=40.0, momentum_score=15.0, reasons=["test"],
    )
    execution.position_amt = 0.01

    result = await engine.run_once_for_symbol(config, "ETHUSDT", _rules(), momentum_score=15.0)
    assert result["action"] == "POSITION_STILL_OPEN"
    assert (config.account_id, "ETHUSDT") in momentum_repo.positions


@pytest.mark.asyncio
async def test_position_closed_via_hard_stop():
    closes, volume = _consolidation_then_breakout()
    klines = _klines(closes, volume)
    engine, momentum_repo, risk_repo, _, execution = _engine(klines)
    config = MomentumConfig()
    momentum_repo.positions[(config.account_id, "ETHUSDT")] = MomentumPosition(
        side="LONG", entry_time=datetime(2026, 1, 1, tzinfo=UTC), entry_price=100.0, quantity=0.01,
        stop_order_id=101, trailing_order_id=102, stop_price=96.0, risk_amount=1.0,
        confluence_score=40.0, momentum_score=15.0, reasons=["test"],
    )
    execution.position_amt = 0.0
    execution.set_order_status(101, "FILLED", avg_price=96.0)
    execution.set_order_status(102, "NEW")

    result = await engine.run_once_for_symbol(config, "ETHUSDT", _rules(), momentum_score=15.0)

    assert result["action"] == "POSITION_CLOSED"
    assert result["trade"].exit_reason == "STOP"
    assert result["trade"].net_pnl < 0
    assert execution.cancelled == [102]
    assert (config.account_id, "ETHUSDT") not in momentum_repo.positions
    assert risk_repo.outcomes == [pytest.approx(result["trade"].net_pnl)]


@pytest.mark.asyncio
async def test_position_closed_via_trailing_stop():
    closes, volume = _consolidation_then_breakout()
    klines = _klines(closes, volume)
    engine, momentum_repo, *_, execution = _engine(klines)
    config = MomentumConfig()
    momentum_repo.positions[(config.account_id, "ETHUSDT")] = MomentumPosition(
        side="LONG", entry_time=datetime(2026, 1, 1, tzinfo=UTC), entry_price=100.0, quantity=0.01,
        stop_order_id=101, trailing_order_id=102, stop_price=96.0, risk_amount=1.0,
        confluence_score=40.0, momentum_score=15.0, reasons=["test"],
    )
    execution.position_amt = 0.0
    execution.set_order_status(101, "CANCELED")
    execution.set_order_status(102, "FILLED", avg_price=115.0)

    result = await engine.run_once_for_symbol(config, "ETHUSDT", _rules(), momentum_score=15.0)

    assert result["action"] == "POSITION_CLOSED"
    assert result["trade"].exit_reason == "TRAILING_STOP"
    assert result["trade"].net_pnl > 0
    assert execution.cancelled == [101]


@pytest.mark.asyncio
async def test_reconciliation_failed_when_neither_leg_shows_filled():
    closes, volume = _consolidation_then_breakout()
    klines = _klines(closes, volume)
    engine, momentum_repo, *_, execution = _engine(klines)
    config = MomentumConfig()
    momentum_repo.positions[(config.account_id, "ETHUSDT")] = MomentumPosition(
        side="LONG", entry_time=datetime(2026, 1, 1, tzinfo=UTC), entry_price=100.0, quantity=0.01,
        stop_order_id=101, trailing_order_id=102, stop_price=96.0, risk_amount=1.0,
        confluence_score=40.0, momentum_score=15.0, reasons=["test"],
    )
    execution.position_amt = 0.0
    execution.set_order_status(101, "CANCELED")
    execution.set_order_status(102, "CANCELED")

    result = await engine.run_once_for_symbol(config, "ETHUSDT", _rules(), momentum_score=15.0)

    assert result["action"] == "RECONCILIATION_FAILED"
    assert (config.account_id, "ETHUSDT") not in momentum_repo.positions
    assert momentum_repo.trades == []


@pytest.mark.asyncio
async def test_kill_switch_blocks_new_entry_without_opening_a_position():
    closes, volume = _consolidation_then_breakout()
    klines = _klines(closes, volume)
    engine, momentum_repo, *_ = _engine(klines, kill_switch_triggered=True)
    config = MomentumConfig(strategy_ids=(STRATEGY_BREAKOUT,), warmup_bars=210)

    result = await engine.run_once_for_symbol(config, "ETHUSDT", _rules(), momentum_score=15.0)

    assert result["action"] == "KILL_SWITCH_BLOCKED"
    assert momentum_repo.positions == {}


@pytest.mark.asyncio
async def test_no_signal_during_flat_consolidation():
    closes, volume = _consolidation_then_breakout(n=280, consolidation_len=280)
    klines = _klines(closes, volume)
    engine, momentum_repo, *_ = _engine(klines)
    config = MomentumConfig(strategy_ids=(STRATEGY_BREAKOUT,), warmup_bars=210)

    result = await engine.run_once_for_symbol(config, "ETHUSDT", _rules(), momentum_score=15.0)
    assert result["action"] in ("NO_SIGNAL", "NO_STOP_AVAILABLE")
    assert momentum_repo.positions == {}


@pytest.mark.asyncio
async def test_entry_blocked_by_risk_engine_with_tiny_equity():
    closes, volume = _consolidation_then_breakout()
    klines = _klines(closes, volume)
    engine, momentum_repo, risk_repo, *_ = _engine(klines, account=_account(equity=0.01))
    config = MomentumConfig(strategy_ids=(STRATEGY_BREAKOUT,), warmup_bars=210)

    result = await engine.run_once_for_symbol(config, "ETHUSDT", _rules(), momentum_score=15.0)

    assert result["action"] == "ENTRY_BLOCKED"
    assert momentum_repo.positions == {}
    assert len(risk_repo.risk_events) == 1
    assert risk_repo.risk_events[0][3].decision != "PASS"


@pytest.mark.asyncio
async def test_entry_opened_persists_position_with_stop_and_trailing_order_ids():
    closes, volume = _consolidation_then_breakout()
    klines = _klines(closes, volume)
    engine, momentum_repo, _, _, execution = _engine(klines)
    config = MomentumConfig(strategy_ids=(STRATEGY_BREAKOUT,), warmup_bars=210)

    result = await engine.run_once_for_symbol(config, "ETHUSDT", _rules(), momentum_score=15.0)

    assert result["action"] == "ENTRY_OPENED"
    position = momentum_repo.positions[(config.account_id, "ETHUSDT")]
    assert position.side == result["side"]
    assert position.stop_order_id != position.trailing_order_id
    assert position.quantity > 0
    assert position.momentum_score == 15.0
    assert len(execution.open_trailing_calls) == 1
    _, side, _, stop_price, callback_rate, leverage, activation_price = execution.open_trailing_calls[0]
    assert callback_rate == config.trailing_callback_rate_pct
    # regression: leverage must be sent to the exchange, not just assumed
    assert leverage == config.leverage
    # the trailing leg must only arm once price has moved in profit - never
    # at (or worse, against) the entry price, or it behaves as an
    # immediate, noise-level stop instead of "let a winner run"
    assert activation_price is not None
    if side == "LONG":
        assert activation_price > position.entry_price
    else:
        assert activation_price < position.entry_price
    # tick-size regression check, same fix as Shadow Trading
    ticks = stop_price / _rules().tick_size
    assert abs(ticks - round(ticks)) < 1e-6
    activation_ticks = activation_price / _rules().tick_size
    assert abs(activation_ticks - round(activation_ticks)) < 1e-6


@pytest.mark.asyncio
async def test_bracket_failure_does_not_persist_a_position():
    closes, volume = _consolidation_then_breakout()
    klines = _klines(closes, volume)
    engine, momentum_repo, _, _, execution = _engine(klines)
    execution.bracket_error = BracketOpenError("stop leg failed", flattened=True)
    config = MomentumConfig(strategy_ids=(STRATEGY_BREAKOUT,), warmup_bars=210)

    result = await engine.run_once_for_symbol(config, "ETHUSDT", _rules(), momentum_score=15.0)

    assert result["action"] == "BRACKET_FAILED"
    assert result["flattened"] is True
    assert momentum_repo.positions == {}
