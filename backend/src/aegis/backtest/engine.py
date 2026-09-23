"""BacktestEngine - spec sections 69/71/74.

The replay loop's one hard rule: a decision at bar `i` only ever sees
`df.iloc[:i+1]` (bars `0..i`, all already closed) - never bar `i+1` or
later. A signal generated from that window fills at bar `i+1`'s OPEN, not
at bar `i`'s own close - using your own decision bar as its own fill price
is a subtle, common lookahead-adjacent mistake (spec section 71: "nunca
assumir execução perfeita"), so entries always cost one bar of latency,
same as a real order would.

Reuses `RiskEngine` (Phase 7) verbatim for sizing and gating - spec section
120: "Toda decisão deve usar o mesmo... RiskEngine... a única diferença
deve ser o ExecutionProvider." A backtest's "ExecutionProvider" is just
this loop's own fill simulation; the risk authority is identical to what
paper/live trading will use.
"""
from __future__ import annotations

from aegis.backtest.metrics import compute_metrics
from aegis.backtest.models import BacktestConfig, BacktestResult, BacktestTrade, OpenPosition
from aegis.execution.fills import apply_slippage as _apply_slippage
from aegis.execution.fills import check_exit as _check_exit
from aegis.execution.fills import close_position
from aegis.execution.fills import compute_stop as _compute_stop
from aegis.execution.fills import compute_take_profit as _compute_take_profit
from aegis.news.conflict import most_recent_status_as_of
from aegis.risk.sizing import round_down_to_step
from aegis.providers.binance.models import SymbolRules
from aegis.risk.kill_switch import evaluate_kill_switch_triggers
from aegis.risk.service import AccountState, RiskEngine, TradeProposal
from aegis.strategy.confluence import combine_signals
from aegis.strategy.strategies import STRATEGY_EVENT_REACTION, evaluate_all
from aegis.technical.service import compute_snapshot

# _apply_slippage/_check_exit/_compute_stop/_compute_take_profit are thin
# re-exports of aegis.execution.fills (Phase 9 extracted the fill/exit math
# so PaperTradingEngine could share it - see that module's docstring for
# why) - kept under their original names here so existing imports/tests
# (`from aegis.backtest.engine import _apply_slippage, ...`) keep working.


def _close_trade(
    position: OpenPosition, exit_time, raw_exit_price: float, exit_reason: str, config: BacktestConfig,
) -> BacktestTrade:
    outcome = close_position(position, raw_exit_price, exit_reason, config.fees_pct, config.slippage_pct)
    return BacktestTrade(
        symbol=config.symbol, side=position.side, entry_time=position.entry_time,
        entry_price=position.entry_price, exit_time=exit_time, exit_price=outcome.exit_price,
        exit_reason=outcome.exit_reason, quantity=position.quantity, gross_pnl=outcome.gross_pnl,
        fees_paid=outcome.fees_paid, net_pnl=outcome.net_pnl, r_multiple=outcome.r_multiple,
        confluence_score=position.confluence_score, reasons=position.reasons,
    )


def _apply_trade_outcome(account: AccountState, pnl: float) -> AccountState:
    """Same state transition `RiskRepository.record_trade_outcome` applies
    in production - duplicated here as pure in-memory math because a
    backtest must never touch the real database's account state."""
    new_equity = account.equity + pnl
    return AccountState(
        equity=new_equity,
        peak_equity=max(account.peak_equity, new_equity),
        daily_starting_equity=account.daily_starting_equity,
        daily_realized_pnl=account.daily_realized_pnl + pnl,
        consecutive_losses=(account.consecutive_losses + 1) if pnl < 0 else 0,
        open_positions_count=0,
        correlated_exposure_pct=account.correlated_exposure_pct,
    )


class BacktestEngine:
    def __init__(self, candle_repo, risk_settings, news_repo=None) -> None:
        self.candle_repo = candle_repo
        self.risk_settings = risk_settings
        self.risk_engine = RiskEngine(risk_settings)
        # Optional on purpose - only STRATEGY_EVENT_REACTION needs it, and
        # every other strategy/backtest must keep working unchanged without
        # a news_repo at all (see `run()`'s check below for what happens if
        # EVENT_REACTION is requested without one - fails loud, not silent).
        self.news_repo = news_repo

    async def run(self, config: BacktestConfig, symbol_rules: SymbolRules, candle_limit: int = 500) -> BacktestResult:
        if STRATEGY_EVENT_REACTION in config.strategy_ids and self.news_repo is None:
            raise ValueError(
                "config.strategy_ids includes EVENT_REACTION but this BacktestEngine has no news_repo - "
                "construct it with news_repo=NewsRepository(pool) to backtest that strategy"
            )

        df = await self.candle_repo.fetch_ohlcv(config.symbol, config.interval, limit=candle_limit, closed_only=True)
        if len(df) < config.warmup_bars + 10:
            raise ValueError(
                f"not enough history for {config.symbol}/{config.interval}: "
                f"{len(df)} candles, need at least {config.warmup_bars + 10}"
            )

        news_history: list[tuple] = []
        if STRATEGY_EVENT_REACTION in config.strategy_ids:
            # Symbol -> asset: strips the USDT quote suffix (BTCUSDT ->
            # BTC) - correct for every asset the News Engine actually
            # tracks (aegis.news.classifier.DEFAULT_ASSET_ALIASES); a
            # symbol whose base isn't tracked there (e.g. 1000PEPEUSDT)
            # simply never has history, so this always returns no status
            # for it rather than guessing - not a bug, just no coverage yet.
            asset = config.symbol.removesuffix("USDT")
            news_history = await self.news_repo.fetch_status_history(
                asset, df["open_time"].iloc[0], df["close_time"].iloc[-1],
            )

        account = AccountState(
            equity=config.initial_equity, peak_equity=config.initial_equity,
            daily_starting_equity=config.initial_equity, daily_realized_pnl=0.0,
            consecutive_losses=0, open_positions_count=0,
        )
        open_position: OpenPosition | None = None
        trades: list[BacktestTrade] = []
        equity_curve: list[tuple] = []

        # Sticky, in-memory only - a backtest must never touch the real
        # KillSwitchRepository/database (spec section 120 boundary, same
        # reasoning as `_apply_trade_outcome`). Once tripped, no further
        # bars can open a new position for the rest of this run - simulating
        # an unrealistic auto-resume would understate how bad a real
        # drawdown/loss-streak blowup would actually be.
        kill_switch_triggered = False
        kill_switch_reasons: list[str] = []
        kill_switch_tripped_at = None

        for i in range(config.warmup_bars, len(df)):
            bar = df.iloc[i]

            if open_position is not None:
                exit_result = _check_exit(open_position, bar)
                if exit_result is not None:
                    exit_price, exit_reason = exit_result
                    trade = _close_trade(open_position, bar["close_time"], exit_price, exit_reason, config)
                    trades.append(trade)
                    account = _apply_trade_outcome(account, trade.net_pnl)
                    open_position = None

                    if not kill_switch_triggered:
                        kill_switch_reasons = evaluate_kill_switch_triggers(
                            account.equity, account.peak_equity, account.consecutive_losses,
                            self.risk_settings.max_drawdown, self.risk_settings.loss_streak_halt_threshold,
                        )
                        if kill_switch_reasons:
                            kill_switch_triggered = True
                            kill_switch_tripped_at = bar["close_time"]

            if open_position is None and not kill_switch_triggered and i + 1 < len(df):
                window = df.iloc[: i + 1]  # bars 0..i only - the hard lookahead boundary
                snapshot = compute_snapshot(config.symbol, config.interval, window)
                # Same boundary applies to news: only a status computed at
                # or before THIS bar's own close may be used - never one
                # computed later, even if it exists in news_history (that
                # would be reading the future).
                news_status = most_recent_status_as_of(news_history, bar["close_time"]) if news_history else None
                signals = evaluate_all(snapshot, config.strategy_ids, news_status=news_status)
                confluence = combine_signals(signals, config.strategy_weights, config.confluence_threshold)

                if confluence.decision != "NO_TRADE":
                    next_bar = df.iloc[i + 1]
                    side = confluence.decision
                    raw_entry = float(next_bar["open"])
                    entry_price = _apply_slippage(raw_entry, side, is_entry=True, slippage_pct=config.slippage_pct)
                    raw_stop_price = _compute_stop(entry_price, snapshot.atr_14, side, config.stop_atr_multiple)

                    if raw_stop_price is not None:
                        # Tick-size rounding (spec rule 151 / realism) - same
                        # fix as Paper/Shadow: a real stop/TP can never sit at
                        # an arbitrary-precision price, so neither should a
                        # backtest's claimed results.
                        stop_price = round_down_to_step(raw_stop_price, symbol_rules.tick_size)
                        take_profit_price = round_down_to_step(
                            _compute_take_profit(entry_price, raw_stop_price, side, config.take_profit_r_multiple),
                            symbol_rules.tick_size,
                        )
                        proposal = TradeProposal(
                            symbol=config.symbol, side=side, entry_price=entry_price, stop_price=stop_price,
                            take_profit_price=take_profit_price, leverage=config.leverage,
                        )
                        risk_decision = self.risk_engine.evaluate(proposal, account, symbol_rules)
                        if risk_decision.decision == "PASS":
                            reasons = [r for sig in signals for r in sig.reasons if sig.signal != "NO_TRADE"]
                            open_position = OpenPosition(
                                side=side, entry_time=next_bar["open_time"], entry_price=entry_price,
                                stop_price=stop_price, take_profit_price=take_profit_price,
                                quantity=risk_decision.position_size.quantity,
                                risk_amount=risk_decision.position_size.risk_amount,
                                confluence_score=confluence.confluence_score, reasons=reasons,
                            )

            unrealized = 0.0
            if open_position is not None:
                if open_position.side == "LONG":
                    unrealized = (bar["close"] - open_position.entry_price) * open_position.quantity
                else:
                    unrealized = (open_position.entry_price - bar["close"]) * open_position.quantity
            equity_curve.append((bar["close_time"], account.equity + unrealized))

        if open_position is not None:
            last_bar = df.iloc[-1]
            trade = _close_trade(open_position, last_bar["close_time"], float(last_bar["close"]),
                                  "END_OF_DATA", config)
            trades.append(trade)
            account = _apply_trade_outcome(account, trade.net_pnl)
            if equity_curve:
                equity_curve[-1] = (equity_curve[-1][0], account.equity)

            if not kill_switch_triggered:
                kill_switch_reasons = evaluate_kill_switch_triggers(
                    account.equity, account.peak_equity, account.consecutive_losses,
                    self.risk_settings.max_drawdown, self.risk_settings.loss_streak_halt_threshold,
                )
                if kill_switch_reasons:
                    kill_switch_triggered = True
                    kill_switch_tripped_at = last_bar["close_time"]

        metrics = compute_metrics(trades, equity_curve, config.initial_equity)
        return BacktestResult(
            config=config, trades=trades, equity_curve=equity_curve, metrics=metrics,
            data_start=df["open_time"].iloc[0], data_end=df["close_time"].iloc[-1], total_bars=len(df),
            kill_switch_triggered=kill_switch_triggered, kill_switch_reasons=kill_switch_reasons,
            kill_switch_tripped_at=kill_switch_tripped_at,
        )
