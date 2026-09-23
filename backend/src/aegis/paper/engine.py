"""PaperTradingEngine - spec sections 9, 120.

The first engine in this codebase that acts on *now* instead of replaying
history. `run_once()` is meant to be called repeatedly (a poller, same
shape as MacroEngine.run_once()/NewsEngine.run_once() from earlier
phases) - each call does at most one thing: check an open position for an
exit, or (if none is open) evaluate whether to open one. It relies on a
persisted cursor (`PaperRepository.get/set_cursor`) to never act twice on
the same already-closed candle.

Reuses the exact same fill/exit math as BacktestEngine
(`aegis.execution.fills`) and the exact same `RiskEngine`/Kill Switch
composition established in Phase 7/7b - spec section 120: "a única
diferença entre paper/live/backtest deve ser o ExecutionProvider". Here,
the "ExecutionProvider" is just "fetch real candles from Postgres and
persist a simulated fill" instead of either replaying historical bars
(Backtest) or placing a real order (a future Live engine).

Known simplification, documented rather than hidden (spec rule 151): entry
fills use the last closed candle's close price (plus the same slippage
model Backtest uses), not a freshly-fetched live ticker price. For BTCUSDT/
ETHUSDT on 1h+ intervals the difference is small, but it is a real
simplification - a future Live engine placing genuine orders would need an
actual current price, not a candle's close.
"""
from __future__ import annotations

from aegis.execution.fills import apply_slippage, check_exit, close_position, compute_stop, compute_take_profit
from aegis.execution.models import OpenPosition
from aegis.paper.models import PaperTrade, PaperTradingConfig
from aegis.providers.binance.models import SymbolRules
from aegis.risk.service import RiskEngine, TradeProposal
from aegis.risk.sizing import round_down_to_step
from aegis.strategy.confluence import combine_signals
from aegis.strategy.strategies import evaluate_all
from aegis.technical.service import compute_snapshot


class PaperTradingEngine:
    def __init__(self, candle_repo, paper_repo, risk_repo, kill_switch_repo, risk_settings) -> None:
        self.candle_repo = candle_repo
        self.paper_repo = paper_repo
        self.risk_repo = risk_repo
        self.kill_switch_repo = kill_switch_repo
        self.risk_settings = risk_settings
        self.risk_engine = RiskEngine(risk_settings)

    async def run_once(self, config: PaperTradingConfig, symbol_rules: SymbolRules) -> dict:
        account_id = config.account_id
        df = await self.candle_repo.fetch_ohlcv(
            config.symbol, config.interval, limit=config.candle_limit, closed_only=True,
        )
        if len(df) < config.warmup_bars + 1:
            return {"action": "NOT_ENOUGH_HISTORY", "candles": len(df), "needed": config.warmup_bars + 1}

        latest_closed = df.iloc[-1]
        cursor = await self.paper_repo.get_cursor(account_id, config.symbol, config.interval)
        if cursor is not None and latest_closed["close_time"] <= cursor:
            return {"action": "NO_NEW_CANDLE", "latest_close_time": latest_closed["close_time"]}

        account = await self.risk_repo.get_account_state(account_id)
        if account is None:
            raise ValueError(f"paper account {account_id!r} not initialized - call initialize_account_state first")

        open_position = await self.paper_repo.get_open_position(account_id, config.symbol)

        if open_position is not None:
            return await self._handle_open_position(config, open_position, latest_closed, account_id)

        return await self._handle_no_open_position(config, df, latest_closed, account, symbol_rules, account_id)

    async def _handle_open_position(self, config: PaperTradingConfig, position: OpenPosition, bar, account_id: str) -> dict:
        exit_result = check_exit(position, bar)
        if exit_result is None:
            await self.paper_repo.set_cursor(account_id, config.symbol, config.interval, bar["close_time"])
            return {"action": "POSITION_STILL_OPEN", "side": position.side, "entry_price": position.entry_price}

        raw_exit_price, exit_reason = exit_result
        outcome = close_position(position, raw_exit_price, exit_reason, config.fees_pct, config.slippage_pct)
        trade = PaperTrade(
            account_id=account_id, symbol=config.symbol, side=position.side,
            entry_time=position.entry_time, entry_price=position.entry_price,
            exit_time=bar["close_time"], exit_price=outcome.exit_price, exit_reason=outcome.exit_reason,
            quantity=position.quantity, gross_pnl=outcome.gross_pnl, fees_paid=outcome.fees_paid,
            net_pnl=outcome.net_pnl, r_multiple=outcome.r_multiple,
            confluence_score=position.confluence_score, reasons=position.reasons,
        )
        await self.paper_repo.record_trade(trade)
        await self.paper_repo.close_position(account_id, config.symbol)
        account = await self.risk_repo.record_trade_outcome(account_id, trade.net_pnl)
        kill_state = await self.kill_switch_repo.check_and_maybe_trigger(
            account_id, account.equity, account.peak_equity, account.consecutive_losses, self.risk_settings,
        )
        await self.paper_repo.set_cursor(account_id, config.symbol, config.interval, bar["close_time"])
        return {
            "action": "POSITION_CLOSED", "trade": trade,
            "kill_switch_triggered": kill_state.is_triggered, "kill_switch_reasons": kill_state.reasons,
        }

    async def _handle_no_open_position(self, config: PaperTradingConfig, df, bar, account, symbol_rules, account_id: str) -> dict:
        # Kill switch is checked BEFORE any signal is even computed - the
        # same block-first composition established in Phase 7b's demo script.
        kill_state = await self.kill_switch_repo.get_state(account_id)
        if kill_state.is_triggered:
            await self.paper_repo.set_cursor(account_id, config.symbol, config.interval, bar["close_time"])
            return {"action": "KILL_SWITCH_BLOCKED", "reasons": kill_state.reasons}

        snapshot = compute_snapshot(config.symbol, config.interval, df)
        signals = evaluate_all(snapshot, config.strategy_ids)
        confluence = combine_signals(signals, config.strategy_weights, config.confluence_threshold)
        await self.paper_repo.set_cursor(account_id, config.symbol, config.interval, bar["close_time"])

        if confluence.decision == "NO_TRADE":
            return {"action": "NO_SIGNAL", "confluence_score": confluence.confluence_score}

        side = confluence.decision
        raw_entry = float(bar["close"])
        entry_price = apply_slippage(raw_entry, side, is_entry=True, slippage_pct=config.slippage_pct)
        raw_stop_price = compute_stop(entry_price, snapshot.atr_14, side, config.stop_atr_multiple)
        if raw_stop_price is None:
            return {"action": "NO_STOP_AVAILABLE", "confluence_score": confluence.confluence_score}
        raw_take_profit_price = compute_take_profit(entry_price, raw_stop_price, side, config.take_profit_r_multiple)
        # Same tick-size rounding as ShadowTradingEngine (spec rule 151 /
        # realism) - a real stop/TP could never sit at an arbitrary-precision
        # price; keeping paper's numbers achievable by a real order too.
        stop_price = round_down_to_step(raw_stop_price, symbol_rules.tick_size)
        take_profit_price = round_down_to_step(raw_take_profit_price, symbol_rules.tick_size)

        proposal = TradeProposal(
            symbol=config.symbol, side=side, entry_price=entry_price, stop_price=stop_price,
            take_profit_price=take_profit_price, leverage=config.leverage,
        )
        risk_decision = self.risk_engine.evaluate(proposal, account, symbol_rules)
        await self.risk_repo.insert_risk_event(account_id, config.symbol, side, risk_decision)
        if risk_decision.decision != "PASS":
            return {"action": "ENTRY_BLOCKED", "reasons": risk_decision.reasons, "confluence_score": confluence.confluence_score}

        reasons = [r for sig in signals for r in sig.reasons if sig.signal != "NO_TRADE"]
        position = OpenPosition(
            side=side, entry_time=bar["close_time"], entry_price=entry_price,
            stop_price=stop_price, take_profit_price=take_profit_price,
            quantity=risk_decision.position_size.quantity, risk_amount=risk_decision.position_size.risk_amount,
            confluence_score=confluence.confluence_score, reasons=reasons,
        )
        await self.paper_repo.open_position(account_id, config.symbol, position)
        return {
            "action": "ENTRY_OPENED", "side": side, "entry_price": entry_price,
            "quantity": position.quantity, "confluence_score": confluence.confluence_score,
        }
