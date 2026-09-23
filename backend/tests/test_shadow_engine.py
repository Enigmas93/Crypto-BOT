"""Tests ShadowTradingEngine.run_once()'s branching logic against fake,
in-memory repos and a fake execution provider - same DI pattern as
test_paper_engine.py. BinanceExecutionProvider itself is separately
verified in test_binance_execution_provider.py; ShadowRepository against
real Postgres in test_shadow_repository_integration.py.
"""
from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest

from aegis.execution.binance_provider import BracketOpenError, BracketOrders
from aegis.providers.binance.models import OrderResult, PositionRisk, SymbolRules
from aegis.risk.kill_switch import evaluate_kill_switch_triggers
from aegis.risk.service import AccountState
from aegis.shadow.engine import ShadowTradingEngine
from aegis.shadow.models import ShadowPosition, ShadowTradingConfig
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
    def __init__(self, df: pd.DataFrame):
        self._df = df

    async def fetch_ohlcv(self, symbol, interval, limit=500, closed_only=True):
        return self._df.tail(limit).reset_index(drop=True)


class _FakeShadowRepo:
    def __init__(self):
        self.positions: dict[tuple[str, str], ShadowPosition] = {}
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
        self.order_statuses: dict[int, OrderResult] = {}
        self.cancelled: list[int] = []
        self.bracket_error: Exception | None = None
        self.open_bracket_calls: list[tuple] = []
        self._next_id = 100

    def set_order_status(self, order_id: int, status: str, avg_price: float = 0.0, update_time_ms: int = 1700000000000):
        self.order_statuses[order_id] = OrderResult(
            order_id=order_id, client_order_id=f"aegis-{order_id}", symbol="BTCUSDT", side="SELL",
            type="STOP_MARKET", status=status, quantity=0.01, executed_qty=0.01 if status == "FILLED" else 0.0,
            avg_price=avg_price, reduce_only=True, close_position=True, stop_price=None,
            update_time_ms=update_time_ms,
        )

    async def get_position(self, symbol):
        return PositionRisk(symbol=symbol, position_amt=self.position_amt, entry_price=0.0, mark_price=0.0,
                             unrealized_pnl=0.0, leverage=3, liquidation_price=0.0)

    async def get_order_status(self, symbol, order_id):
        return self.order_statuses.get(order_id) or OrderResult(
            order_id=order_id, client_order_id="x", symbol=symbol, side="SELL", type="STOP_MARKET",
            status="NEW", quantity=0.01, executed_qty=0.0, avg_price=0.0, reduce_only=True,
            close_position=True, stop_price=None, update_time_ms=1700000000000,
        )

    async def cancel_leftover_order(self, symbol, order_id):
        self.cancelled.append(order_id)

    async def open_bracket_position(self, symbol, side, quantity, stop_price, take_profit_price, leverage):
        self.open_bracket_calls.append((symbol, side, quantity, stop_price, take_profit_price, leverage))
        if self.bracket_error is not None:
            raise self.bracket_error
        self._next_id += 1
        entry_id = self._next_id
        self._next_id += 1
        stop_id = self._next_id
        self._next_id += 1
        tp_id = self._next_id
        entry = OrderResult(order_id=entry_id, client_order_id="e", symbol=symbol,
                             side="BUY" if side == "LONG" else "SELL", type="MARKET", status="FILLED",
                             quantity=quantity, executed_qty=quantity, avg_price=stop_price,  # placeholder fill price
                             reduce_only=False, close_position=False, stop_price=None, update_time_ms=1700000000000)
        stop = OrderResult(order_id=stop_id, client_order_id="s", symbol=symbol,
                            side="SELL" if side == "LONG" else "BUY", type="STOP_MARKET", status="NEW",
                            quantity=quantity, executed_qty=0.0, avg_price=0.0, reduce_only=True,
                            close_position=True, stop_price=stop_price, update_time_ms=1700000000000)
        tp = OrderResult(order_id=tp_id, client_order_id="t", symbol=symbol,
                          side="SELL" if side == "LONG" else "BUY", type="TAKE_PROFIT_MARKET", status="NEW",
                          quantity=quantity, executed_qty=0.0, avg_price=0.0, reduce_only=True,
                          close_position=True, stop_price=take_profit_price, update_time_ms=1700000000000)
        return BracketOrders(entry=entry, stop=stop, take_profit=tp)


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
    shadow_repo = _FakeShadowRepo()
    risk_repo = _FakeRiskRepo(account or _account())
    kill_switch_repo = _FakeKillSwitchRepo(pre_triggered=kill_switch_triggered)
    execution = _FakeExecutionProvider()
    engine = ShadowTradingEngine(candle_repo, shadow_repo, risk_repo, kill_switch_repo, execution, _Settings())
    return engine, shadow_repo, risk_repo, kill_switch_repo, execution


@pytest.mark.asyncio
async def test_raises_when_account_not_initialized():
    closes, volume = _consolidation_then_breakout()
    df = _candles_df(closes, volume)

    class _NoAccountRiskRepo:
        async def get_account_state(self, account_id):
            return None

    engine = ShadowTradingEngine(_FakeCandleRepo(df), _FakeShadowRepo(), _NoAccountRiskRepo(),
                                  _FakeKillSwitchRepo(), _FakeExecutionProvider(), _Settings())
    config = ShadowTradingConfig(symbol="BTCUSDT", interval="1h")
    with pytest.raises(ValueError):
        await engine.run_once(config, _rules())


@pytest.mark.asyncio
async def test_not_enough_history():
    closes, volume = _consolidation_then_breakout(n=50, consolidation_len=40)
    df = _candles_df(closes, volume)
    engine, *_ = _engine(df)
    config = ShadowTradingConfig(symbol="BTCUSDT", interval="1h", warmup_bars=210)

    result = await engine.run_once(config, _rules())
    assert result["action"] == "NOT_ENOUGH_HISTORY"


@pytest.mark.asyncio
async def test_no_new_candle_when_cursor_already_at_latest_close():
    closes, volume = _consolidation_then_breakout()
    df = _candles_df(closes, volume)
    engine, shadow_repo, *_ = _engine(df)
    config = ShadowTradingConfig(symbol="BTCUSDT", interval="1h", strategy_ids=(STRATEGY_BREAKOUT,), warmup_bars=210)
    shadow_repo.cursors[(config.account_id, config.symbol, config.interval)] = df["close_time"].iloc[-1]

    result = await engine.run_once(config, _rules())
    assert result["action"] == "NO_NEW_CANDLE"


@pytest.mark.asyncio
async def test_position_still_open_when_exchange_reports_nonzero_amount():
    closes, volume = _consolidation_then_breakout()
    df = _candles_df(closes, volume)
    engine, shadow_repo, _, _, execution = _engine(df)
    config = ShadowTradingConfig(symbol="BTCUSDT", interval="1h")
    shadow_repo.positions[(config.account_id, config.symbol)] = ShadowPosition(
        side="LONG", entry_time=df["open_time"].iloc[-5], entry_price=100.0, quantity=0.01,
        stop_order_id=101, take_profit_order_id=102, stop_price=96.0, take_profit_price=108.0,
        risk_amount=1.0, confluence_score=40.0, reasons=["test"],
    )
    execution.position_amt = 0.01  # still open on the exchange

    result = await engine.run_once(config, _rules())
    assert result["action"] == "POSITION_STILL_OPEN"
    assert (config.account_id, config.symbol) in shadow_repo.positions


@pytest.mark.asyncio
async def test_position_closed_via_stop_records_trade_and_cancels_leftover():
    closes, volume = _consolidation_then_breakout()
    df = _candles_df(closes, volume)
    engine, shadow_repo, risk_repo, kill_switch_repo, execution = _engine(df)
    config = ShadowTradingConfig(symbol="BTCUSDT", interval="1h")
    shadow_repo.positions[(config.account_id, config.symbol)] = ShadowPosition(
        side="LONG", entry_time=df["open_time"].iloc[-5], entry_price=100.0, quantity=0.01,
        stop_order_id=101, take_profit_order_id=102, stop_price=96.0, take_profit_price=108.0,
        risk_amount=1.0, confluence_score=40.0, reasons=["test"],
    )
    execution.position_amt = 0.0  # flat now
    execution.set_order_status(101, "FILLED", avg_price=96.0)
    execution.set_order_status(102, "NEW")

    result = await engine.run_once(config, _rules())

    assert result["action"] == "POSITION_CLOSED"
    assert result["trade"].exit_reason == "STOP"
    assert result["trade"].net_pnl < 0
    assert execution.cancelled == [102]  # the take-profit leg was cancelled
    assert (config.account_id, config.symbol) not in shadow_repo.positions
    assert len(shadow_repo.trades) == 1
    assert risk_repo.outcomes == [pytest.approx(result["trade"].net_pnl)]


@pytest.mark.asyncio
async def test_position_closed_via_take_profit():
    closes, volume = _consolidation_then_breakout()
    df = _candles_df(closes, volume)
    engine, shadow_repo, *_ , execution = _engine(df)
    config = ShadowTradingConfig(symbol="BTCUSDT", interval="1h")
    shadow_repo.positions[(config.account_id, config.symbol)] = ShadowPosition(
        side="LONG", entry_time=df["open_time"].iloc[-5], entry_price=100.0, quantity=0.01,
        stop_order_id=101, take_profit_order_id=102, stop_price=96.0, take_profit_price=108.0,
        risk_amount=1.0, confluence_score=40.0, reasons=["test"],
    )
    execution.position_amt = 0.0
    execution.set_order_status(101, "CANCELED")
    execution.set_order_status(102, "FILLED", avg_price=108.0)

    result = await engine.run_once(config, _rules())

    assert result["action"] == "POSITION_CLOSED"
    assert result["trade"].exit_reason == "TAKE_PROFIT"
    assert result["trade"].net_pnl > 0
    assert execution.cancelled == [101]


@pytest.mark.asyncio
async def test_reconciliation_failed_when_neither_leg_shows_filled():
    closes, volume = _consolidation_then_breakout()
    df = _candles_df(closes, volume)
    engine, shadow_repo, *_ , execution = _engine(df)
    config = ShadowTradingConfig(symbol="BTCUSDT", interval="1h")
    shadow_repo.positions[(config.account_id, config.symbol)] = ShadowPosition(
        side="LONG", entry_time=df["open_time"].iloc[-5], entry_price=100.0, quantity=0.01,
        stop_order_id=101, take_profit_order_id=102, stop_price=96.0, take_profit_price=108.0,
        risk_amount=1.0, confluence_score=40.0, reasons=["test"],
    )
    execution.position_amt = 0.0
    execution.set_order_status(101, "CANCELED")
    execution.set_order_status(102, "CANCELED")

    result = await engine.run_once(config, _rules())

    assert result["action"] == "RECONCILIATION_FAILED"
    assert (config.account_id, config.symbol) not in shadow_repo.positions  # stops blocking new entries
    assert shadow_repo.trades == []  # never fabricates a trade record


@pytest.mark.asyncio
async def test_kill_switch_blocks_new_entry_without_opening_a_position():
    closes, volume = _consolidation_then_breakout()
    df = _candles_df(closes, volume)
    engine, shadow_repo, *_ = _engine(df, kill_switch_triggered=True)
    config = ShadowTradingConfig(symbol="BTCUSDT", interval="1h", strategy_ids=(STRATEGY_BREAKOUT,), warmup_bars=210)

    result = await engine.run_once(config, _rules())

    assert result["action"] == "KILL_SWITCH_BLOCKED"
    assert shadow_repo.positions == {}


@pytest.mark.asyncio
async def test_no_signal_during_flat_consolidation():
    closes, volume = _consolidation_then_breakout(n=280, consolidation_len=280)
    df = _candles_df(closes, volume)
    engine, shadow_repo, *_ = _engine(df)
    config = ShadowTradingConfig(symbol="BTCUSDT", interval="1h", strategy_ids=(STRATEGY_BREAKOUT,), warmup_bars=210)

    result = await engine.run_once(config, _rules())
    assert result["action"] in ("NO_SIGNAL", "NO_STOP_AVAILABLE")
    assert shadow_repo.positions == {}


@pytest.mark.asyncio
async def test_entry_blocked_by_risk_engine_with_tiny_equity():
    closes, volume = _consolidation_then_breakout(n=261, consolidation_len=260)
    df = _candles_df(closes, volume)
    engine, shadow_repo, risk_repo, *_ = _engine(df, account=_account(equity=0.01))
    config = ShadowTradingConfig(symbol="BTCUSDT", interval="1h", strategy_ids=(STRATEGY_BREAKOUT,), warmup_bars=210)

    result = await engine.run_once(config, _rules())

    assert result["action"] == "ENTRY_BLOCKED"
    assert shadow_repo.positions == {}
    # A BLOCKED decision must still be logged to the audit trail, not just
    # a successful one - otherwise there's no record of why a signal never
    # became a trade.
    assert len(risk_repo.risk_events) == 1
    assert risk_repo.risk_events[0][3].decision != "PASS"


@pytest.mark.asyncio
async def test_entry_opened_persists_shadow_position_with_real_order_ids():
    closes, volume = _consolidation_then_breakout(n=261, consolidation_len=260)
    df = _candles_df(closes, volume)
    engine, shadow_repo, _, _, execution = _engine(df)
    config = ShadowTradingConfig(symbol="BTCUSDT", interval="1h", strategy_ids=(STRATEGY_BREAKOUT,), warmup_bars=210)

    result = await engine.run_once(config, _rules())

    assert result["action"] == "ENTRY_OPENED"
    position = shadow_repo.positions[(config.account_id, config.symbol)]
    assert position.side == result["side"]
    assert position.stop_order_id != position.take_profit_order_id
    assert position.quantity > 0
    # regression: leverage must be sent to the exchange, not just assumed
    # (found live: SOLUSDT stayed at the account's pre-existing leverage,
    # 20x, because nothing ever called set_leverage before this fix)
    assert execution.open_bracket_calls[0][-1] == config.leverage


@pytest.mark.asyncio
async def test_entry_stop_and_take_profit_prices_are_rounded_to_tick_size():
    """Regression test: caught live on ETHUSDT - Binance rejected the real
    order with -1111 'Precision is over the maximum defined for this asset'
    because the ATR-derived stop/TP price had more decimal places than the
    symbol's tick_size allows. _rules() (tick_size=0.1) wouldn't reproduce
    this - a tighter tick_size (0.01, ETHUSDT's real one) is needed to prove
    the fix actually rounds instead of passing the raw float through."""
    closes, volume = _consolidation_then_breakout(n=261, consolidation_len=260)
    df = _candles_df(closes, volume)
    engine, shadow_repo, _, _, execution = _engine(df)
    tight_rules = SymbolRules(symbol="ETHUSDT", status="TRADING", price_precision=2, quantity_precision=3,
                               tick_size=0.01, step_size=0.001, min_notional=5.0)
    config = ShadowTradingConfig(symbol="ETHUSDT", interval="1h", strategy_ids=(STRATEGY_BREAKOUT,), warmup_bars=210)

    result = await engine.run_once(config, tight_rules)

    assert result["action"] == "ENTRY_OPENED"
    assert len(execution.open_bracket_calls) == 1
    _, _, _, stop_price, take_profit_price, _ = execution.open_bracket_calls[0]
    # a value is tick-aligned iff dividing by the tick size lands on (very
    # close to) a whole number - not just "has <= 2 decimals" by accident
    for price in (stop_price, take_profit_price):
        ticks = price / tight_rules.tick_size
        assert abs(ticks - round(ticks)) < 1e-6, f"{price} is not a multiple of tick_size={tight_rules.tick_size}"


@pytest.mark.asyncio
async def test_bracket_failure_does_not_persist_a_position():
    closes, volume = _consolidation_then_breakout(n=261, consolidation_len=260)
    df = _candles_df(closes, volume)
    engine, shadow_repo, _, _, execution = _engine(df)
    execution.bracket_error = BracketOpenError("stop leg failed", flattened=True)
    config = ShadowTradingConfig(symbol="BTCUSDT", interval="1h", strategy_ids=(STRATEGY_BREAKOUT,), warmup_bars=210)

    result = await engine.run_once(config, _rules())

    assert result["action"] == "BRACKET_FAILED"
    assert result["flattened"] is True
    assert shadow_repo.positions == {}
