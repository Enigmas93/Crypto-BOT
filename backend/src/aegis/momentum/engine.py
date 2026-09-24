"""MomentumTradingEngine - Fase 14 ("moonshot" scanner).

Built after an explicit conversation with the user about risk: chasing
tokens that "surgem com muita força e fazem uma quantidade vezes alta"
(emerge with force and multiply many times over) is a fundamentally
higher-risk style of trading than every other engine in this system - thin
liquidity, manipulation, gap risk and delisting risk are all real for
genuinely new/thin listings. The user explicitly chose to scope this to
LIQUID pairs only (`MomentumConfig.min_quote_volume` is a real floor, not
a suggestion) rather than chasing brand-new/thin listings - this engine
must never be pointed at a symbol universe without that floor enforced by
`aegis.scanner.ranking.rank_by_momentum`.

Same decision pipeline as Shadow Trading (Phase 10): Kill Switch, Strategy
Engine, real RiskEngine, real orders via BinanceExecutionProvider. Three
things differ:

1. Symbol universe is dynamic (the Scanner's current top-N), not a fixed
   config list - so candles are fetched directly over REST each cycle
   (`aegis.momentum.candles.klines_to_closed_dataframe`), not read from the
   collector's persisted `candles` table, which only covers a fixed
   pre-subscribed symbol set.
2. Exit is a hard safety-net stop + a native Binance TRAILING_STOP_MARKET,
   not a fixed take-profit target - letting a genuinely strong move run
   instead of capping it, which is the entire point of this engine.
3. An open position is reconciled even if its symbol drops out of the
   current scan's top-N - a position already open is managed until it
   closes, never abandoned just because it's no longer "hot".
"""
from __future__ import annotations

from datetime import UTC, datetime

from aegis.execution.binance_provider import BracketOpenError
from aegis.execution.fills import apply_slippage, close_position, compute_stop
from aegis.execution.models import OpenPosition
from aegis.momentum.candles import klines_to_closed_dataframe
from aegis.momentum.models import MomentumConfig, MomentumPosition, MomentumTrade
from aegis.risk.service import RiskEngine, TradeProposal
from aegis.risk.sizing import round_down_to_step
from aegis.scanner.ranking import rank_by_momentum
from aegis.strategy.confluence import combine_signals
from aegis.strategy.strategies import evaluate_all
from aegis.technical.service import compute_snapshot


class MomentumTradingEngine:
    def __init__(self, rest_client, momentum_repo, risk_repo, kill_switch_repo, execution, risk_settings) -> None:
        self.rest = rest_client
        self.momentum_repo = momentum_repo
        self.risk_repo = risk_repo
        self.kill_switch_repo = kill_switch_repo
        self.execution = execution
        self.risk_settings = risk_settings
        self.risk_engine = RiskEngine(risk_settings)

    async def scan(self, config: MomentumConfig, exclude: set[str]):
        tickers = await self.rest.get_24h_tickers()
        return rank_by_momentum(tickers, config.min_quote_volume, exclude=exclude, top_n=config.top_n)

    async def run_once_for_symbol(self, config: MomentumConfig, symbol: str, symbol_rules, momentum_score: float) -> dict:
        account_id = config.account_id
        account = await self.risk_repo.get_account_state(account_id)
        if account is None:
            raise ValueError(f"momentum account {account_id!r} not initialized - call initialize_account_state first")

        open_position = await self.momentum_repo.get_open_position(account_id, symbol)
        if open_position is not None:
            return await self._handle_open_position(config, symbol, open_position, account_id)

        return await self._handle_no_open_position(config, symbol, symbol_rules, momentum_score, account, account_id)

    async def _handle_open_position(self, config: MomentumConfig, symbol: str, position: MomentumPosition, account_id: str) -> dict:
        live_position = await self.execution.get_position(symbol)
        if live_position.position_amt != 0:
            return {"action": "POSITION_STILL_OPEN", "side": position.side, "entry_price": position.entry_price}

        stop_status = await self.execution.get_order_status(symbol, position.stop_order_id)
        trailing_status = await self.execution.get_order_status(symbol, position.trailing_order_id)

        if stop_status.status == "FILLED":
            filled_order, leftover_order_id, exit_reason = stop_status, position.trailing_order_id, "STOP"
        elif trailing_status.status == "FILLED":
            filled_order, leftover_order_id, exit_reason = trailing_status, position.stop_order_id, "TRAILING_STOP"
        else:
            # Never fabricate a price - same principle as ShadowTradingEngine.
            await self.momentum_repo.close_position(account_id, symbol)
            await self.risk_repo.set_exposure(account_id, len(await self.momentum_repo.get_open_symbols(account_id)))
            return {
                "action": "RECONCILIATION_FAILED", "stop_status": stop_status.status,
                "trailing_status": trailing_status.status,
                "message": "position is flat on the exchange but neither leg shows FILLED - manual review needed",
            }

        await self.execution.cancel_leftover_order(symbol, leftover_order_id)

        outcome = close_position(
            OpenPosition(side=position.side, entry_time=position.entry_time, entry_price=position.entry_price,
                         stop_price=position.stop_price, take_profit_price=position.stop_price,  # unused for exit math here
                         quantity=position.quantity, risk_amount=position.risk_amount,
                         confluence_score=position.confluence_score, reasons=position.reasons),
            raw_exit_price=filled_order.avg_price, exit_reason=exit_reason,
            fees_pct=config.fees_pct, slippage_pct=0.0,
        )
        exit_time = datetime.fromtimestamp(filled_order.update_time_ms / 1000, tz=UTC)
        trade = MomentumTrade(
            account_id=account_id, symbol=symbol, side=position.side,
            entry_time=position.entry_time, entry_price=position.entry_price,
            exit_time=exit_time, exit_price=outcome.exit_price, exit_reason=outcome.exit_reason,
            quantity=position.quantity, gross_pnl=outcome.gross_pnl, fees_paid=outcome.fees_paid,
            net_pnl=outcome.net_pnl, r_multiple=outcome.r_multiple,
            confluence_score=position.confluence_score, momentum_score=position.momentum_score,
            reasons=position.reasons,
        )
        await self.momentum_repo.record_trade(trade)
        await self.momentum_repo.close_position(account_id, symbol)
        await self.risk_repo.set_exposure(account_id, len(await self.momentum_repo.get_open_symbols(account_id)))
        account = await self.risk_repo.record_trade_outcome(account_id, trade.net_pnl)
        kill_state = await self.kill_switch_repo.check_and_maybe_trigger(
            account_id, account.equity, account.peak_equity, account.consecutive_losses, self.risk_settings,
        )
        return {
            "action": "POSITION_CLOSED", "trade": trade,
            "kill_switch_triggered": kill_state.is_triggered, "kill_switch_reasons": kill_state.reasons,
        }

    async def _handle_no_open_position(
        self, config: MomentumConfig, symbol: str, symbol_rules, momentum_score: float, account, account_id: str,
    ) -> dict:
        klines = await self.rest.get_klines(symbol, config.interval, limit=config.candle_limit)
        df = klines_to_closed_dataframe(klines)
        if len(df) < config.warmup_bars + 1:
            return {"action": "NOT_ENOUGH_HISTORY", "candles": len(df), "needed": config.warmup_bars + 1}

        latest_closed = df.iloc[-1]
        cursor = await self.momentum_repo.get_cursor(account_id, symbol, config.interval)
        if cursor is not None and latest_closed["close_time"] <= cursor:
            return {"action": "NO_NEW_CANDLE", "latest_close_time": latest_closed["close_time"]}

        kill_state = await self.kill_switch_repo.get_state(account_id)
        if kill_state.is_triggered:
            await self.momentum_repo.set_cursor(account_id, symbol, config.interval, latest_closed["close_time"])
            return {"action": "KILL_SWITCH_BLOCKED", "reasons": kill_state.reasons}

        snapshot = compute_snapshot(symbol, config.interval, df)
        signals = evaluate_all(snapshot, config.strategy_ids)
        confluence = combine_signals(signals, config.strategy_weights, config.confluence_threshold)
        await self.momentum_repo.set_cursor(account_id, symbol, config.interval, latest_closed["close_time"])

        if confluence.decision == "NO_TRADE":
            return {"action": "NO_SIGNAL", "confluence_score": confluence.confluence_score}

        side = confluence.decision
        reference_price = float(latest_closed["close"])
        raw_stop_price = compute_stop(reference_price, snapshot.atr_14, side, config.stop_atr_multiple)
        if raw_stop_price is None:
            return {"action": "NO_STOP_AVAILABLE", "confluence_score": confluence.confluence_score}
        stop_price = round_down_to_step(raw_stop_price, symbol_rules.tick_size)

        activation_multiplier = (
            1 + config.trailing_activation_pct / 100 if side == "LONG" else 1 - config.trailing_activation_pct / 100
        )
        activation_price = round_down_to_step(reference_price * activation_multiplier, symbol_rules.tick_size)

        # No take_profit_price: this engine's profit-taking is the trailing
        # stop, not a pre-set target - RiskEngine's BAD_RISK_REWARD check is
        # a no-op here by design (it only runs when take_profit_price is
        # given), since a trailing exit's eventual R-multiple is inherently
        # unknown at entry time, not something worth faking a number for.
        proposal = TradeProposal(
            symbol=symbol, side=side, entry_price=reference_price, stop_price=stop_price,
            take_profit_price=None, leverage=config.leverage,
        )
        risk_decision = self.risk_engine.evaluate(proposal, account, symbol_rules)
        await self.risk_repo.insert_risk_event(account_id, symbol, side, risk_decision)
        if risk_decision.decision != "PASS":
            return {"action": "ENTRY_BLOCKED", "reasons": risk_decision.reasons, "confluence_score": confluence.confluence_score}

        quantity = risk_decision.position_size.quantity
        try:
            brackets = await self.execution.open_trailing_bracket_position(
                symbol, side, quantity, stop_price, config.trailing_callback_rate_pct, config.leverage,
                activation_price=activation_price,
            )
        except BracketOpenError as exc:
            return {
                "action": "BRACKET_FAILED", "flattened": exc.flattened, "error": str(exc),
                "confluence_score": confluence.confluence_score,
            }

        reasons = [r for sig in signals for r in sig.reasons if sig.signal != "NO_TRADE"]
        entry_price = brackets.entry.avg_price or apply_slippage(reference_price, side, is_entry=True, slippage_pct=0.0)
        position = MomentumPosition(
            side=side, entry_time=latest_closed["close_time"], entry_price=entry_price,
            quantity=brackets.entry.executed_qty or quantity,
            stop_order_id=brackets.stop.order_id, trailing_order_id=brackets.trailing_stop.order_id,
            stop_price=stop_price, risk_amount=risk_decision.position_size.risk_amount,
            confluence_score=confluence.confluence_score, momentum_score=momentum_score, reasons=reasons,
        )
        await self.momentum_repo.open_position(account_id, symbol, position)
        await self.risk_repo.set_exposure(account_id, len(await self.momentum_repo.get_open_symbols(account_id)))
        return {
            "action": "ENTRY_OPENED", "side": side, "entry_price": entry_price,
            "quantity": position.quantity, "confluence_score": confluence.confluence_score,
        }
