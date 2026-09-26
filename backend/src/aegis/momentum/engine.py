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

from aegis.execution.fills import apply_slippage, close_position, compute_stop
from aegis.execution.models import BracketOpenError, OpenPosition
from aegis.momentum.candles import klines_to_closed_dataframe
from aegis.momentum.models import MomentumConfig, MomentumPosition, MomentumTrade
from aegis.risk.correlation import compute_account_correlated_exposure_from_rest
from aegis.risk.service import RiskEngine, TradeProposal
from aegis.risk.sizing import round_down_to_step
from aegis.scanner.ranking import rank_by_momentum
from aegis.strategy.confluence import combine_signals
from aegis.strategy.strategies import evaluate_all
from aegis.technical.service import compute_snapshot


class MomentumTradingEngine:
    def __init__(
        self, rest_client, momentum_repo, risk_repo, kill_switch_repo, execution, risk_settings,
        strategy_settings_repo=None, capital_allocation_repo=None, capital_allocation_key=None,
    ) -> None:
        self.rest = rest_client
        self.momentum_repo = momentum_repo
        self.risk_repo = risk_repo
        self.kill_switch_repo = kill_switch_repo
        self.execution = execution
        self.risk_settings = risk_settings
        self.risk_engine = RiskEngine(risk_settings)
        # Optional (Fase 17f) - per-strategy enable/disable toggle. None
        # means "no filtering", matching every existing caller.
        self.strategy_settings_repo = strategy_settings_repo
        # Optional (Fase 17g) - see ShadowTradingEngine's identical fields
        # for why (splits one shared real BingX balance between siblings).
        self.capital_allocation_repo = capital_allocation_repo
        self.capital_allocation_key = capital_allocation_key

    async def _effective_strategy_ids(self, strategy_ids: tuple[str, ...]) -> tuple[str, ...]:
        if self.strategy_settings_repo is None:
            return strategy_ids
        return await self.strategy_settings_repo.filter_enabled(strategy_ids)

    async def sync_equity(self, account_id: str) -> None:
        """Call once per poll cycle (not once per symbol), before
        evaluating any entry - Fase 17b's real-money position-sizing safety
        fix. See ShadowTradingEngine.sync_equity / BingXExecutionProvider.
        get_equity / RiskRepository.sync_equity_from_exchange for why.
        Scaled by capital_allocation_pct (Fase 17g) when configured."""
        real_equity = await self.execution.get_equity()
        if self.capital_allocation_repo is not None and self.capital_allocation_key is not None:
            allocation_pct = await self.capital_allocation_repo.get_allocation(self.capital_allocation_key)
            real_equity *= allocation_pct
        await self.risk_repo.sync_equity_from_exchange(account_id, real_equity)

    async def _update_exposure(self, account_id: str, interval: str) -> None:
        """Recomputes open_positions_count AND correlated_exposure_pct
        (PortfolioCorrelationEngine, Fase 17) every time a position opens or
        closes. Momentum's universe is dynamic and not guaranteed to be in
        the collector's persisted candles table, so correlation is computed
        straight from this engine's own REST client (Binance or BingX -
        both implement get_klines the same way), not CandleRepository."""
        open_symbols = await self.momentum_repo.get_open_symbols(account_id)
        correlation = await compute_account_correlated_exposure_from_rest(self.rest, open_symbols, interval)
        await self.risk_repo.set_exposure(account_id, len(open_symbols), correlation.correlated_exposure_pct)

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
            await self._update_exposure(account_id, config.interval)
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
        await self._update_exposure(account_id, config.interval)
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

        # Fase 17k: reject a candidate that already looks "tired" rather
        # than near the start of its move - chasing an already-extended
        # leg, not catching one early (see MomentumConfig's docstring for
        # the real trade data both checks below were calibrated against).
        # Checked before strategy evaluation: no confluence result changes
        # whether a leg has already run too far to chase. Two independent
        # signals, either one is disqualifying:
        #   1. price extension: the recent move already spent several ATRs
        #      of range.
        #   2. climax volume: an extreme volume spike at entry, the
        #      classic blow-off-top signature, catches cases the price
        #      check alone might miss (a big move compressed into fewer
        #      candles than extension_lookback_bars covers).
        extended_reasons: list[str] = []
        extension_ratio = None
        lookback = config.extension_lookback_bars
        if lookback > 0 and snapshot.atr_14 and len(df) > lookback:
            recent_move = abs(float(df["close"].iloc[-1]) - float(df["close"].iloc[-1 - lookback]))
            extension_ratio = round(recent_move / snapshot.atr_14, 2)
            if extension_ratio > config.extension_atr_multiple:
                extended_reasons.append("PRICE_EXTENDED")
        if (
            config.climax_volume_zscore is not None
            and snapshot.volume_zscore_20 is not None
            and snapshot.volume_zscore_20 > config.climax_volume_zscore
        ):
            extended_reasons.append("CLIMAX_VOLUME")
        if extended_reasons:
            await self.momentum_repo.set_cursor(account_id, symbol, config.interval, latest_closed["close_time"])
            return {
                "action": "ALREADY_EXTENDED", "reasons": extended_reasons, "extension_ratio": extension_ratio,
                "volume_zscore": snapshot.volume_zscore_20,
            }

        strategy_ids = await self._effective_strategy_ids(config.strategy_ids)
        signals = evaluate_all(snapshot, strategy_ids)
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

        # Fase 17j: activation/callback distances scale with THIS symbol's
        # own ATR%, not one fixed percentage for every candidate - see
        # MomentumConfig's docstring for why (a fixed 1.5%/2.0% arms/closes
        # almost instantly on BingX's wilder micro-caps, on nothing more
        # than ordinary noise).
        atr_pct = (snapshot.atr_14 / reference_price * 100) if snapshot.atr_14 else 0.0
        activation_pct = min(
            max(atr_pct * config.trailing_activation_atr_multiple, config.trailing_activation_min_pct),
            config.trailing_activation_max_pct,
        )
        callback_pct = min(
            max(atr_pct * config.trailing_callback_atr_multiple, config.trailing_callback_min_pct),
            config.trailing_callback_max_pct,
        )
        activation_multiplier = 1 + activation_pct / 100 if side == "LONG" else 1 - activation_pct / 100
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
                symbol, side, quantity, stop_price, callback_pct, config.leverage,
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
        await self._update_exposure(account_id, config.interval)
        return {
            "action": "ENTRY_OPENED", "side": side, "entry_price": entry_price,
            "quantity": position.quantity, "confluence_score": confluence.confluence_score,
        }
