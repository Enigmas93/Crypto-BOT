"""ShadowTradingEngine - spec sections 9, 120.

Same decision pipeline as `PaperTradingEngine` (Phase 9): check the Kill
Switch, evaluate the Strategy Engine, gate through the real `RiskEngine` -
but instead of simulating a fill in memory, it places REAL orders via
`BinanceExecutionProvider` (testnet by default). This is the first engine
whose notion of "is a position still open" comes from asking the exchange
(`get_position`), not from trusting local state - a real stop-loss or
take-profit order can fill at any moment, entirely outside this engine's
control, so `run_once()` treats "the exchange says flat" as the source of
truth and reconciles local records to match, never the other way around.

Exit detection differs fundamentally from Paper Trading: Paper checks a
candle's high/low once per poll cycle (a batch, backtest-like check).
Shadow's stop/take-profit are real exchange-side orders that trigger
immediately and automatically whenever price crosses them - this engine's
job on each poll is just to notice that happened and figure out which leg
fired, not to decide whether it should happen.
"""
from __future__ import annotations

from datetime import UTC, datetime

from aegis.execution.fills import apply_slippage, close_position, compute_stop, compute_take_profit
from aegis.execution.models import BracketOpenError, OpenPosition
from aegis.risk.correlation import compute_account_correlated_exposure_from_candles
from aegis.risk.service import RiskEngine, TradeProposal
from aegis.risk.sizing import round_down_to_step
from aegis.shadow.models import ShadowPosition, ShadowTrade, ShadowTradingConfig
from aegis.strategy.confluence import combine_signals
from aegis.strategy.market_context import fetch_derivatives_snapshot, fetch_liquidation_snapshot
from aegis.strategy.strategies import evaluate_all
from aegis.technical.service import compute_snapshot


class ShadowTradingEngine:
    def __init__(
        self, candle_repo, shadow_repo, risk_repo, kill_switch_repo, execution, risk_settings,
        derivatives_repo=None, liquidation_repo=None, strategy_settings_repo=None,
        capital_allocation_repo=None, capital_allocation_key=None,
    ) -> None:
        self.candle_repo = candle_repo
        self.shadow_repo = shadow_repo
        self.risk_repo = risk_repo
        self.kill_switch_repo = kill_switch_repo
        self.execution = execution
        self.risk_settings = risk_settings
        self.risk_engine = RiskEngine(risk_settings)
        # Optional (Fase 17e) - only STRATEGY_LIQUIDATION_SQUEEZE needs
        # these, and only for Binance's fixed symbol universe (where
        # DerivativesEngine/LiquidationEngine actually collect data). None
        # for shadow_bingx: BingX has no derivatives/liquidation collection
        # yet, so the strategy correctly stays inert (NO_TRADE) there
        # rather than erroring - see aegis.strategy.market_context.
        self.derivatives_repo = derivatives_repo
        self.liquidation_repo = liquidation_repo
        # Optional (Fase 17f) - per-strategy enable/disable toggle from the
        # dashboard. None means "no filtering" (every ALL_STRATEGY_IDS
        # member stays active), matching every existing test/caller that
        # doesn't pass this.
        self.strategy_settings_repo = strategy_settings_repo
        # Optional (Fase 17g) - splits a shared real BingX balance between
        # this engine and its sibling (e.g. shadow_bingx vs momentum_bingx)
        # so neither sizes a position assuming it alone owns the whole
        # account - see migration 0021's docstring. None/no key means no
        # scaling (100% of get_equity()), matching Binance/Paper, which
        # never share a real balance with anything.
        self.capital_allocation_repo = capital_allocation_repo
        self.capital_allocation_key = capital_allocation_key

    async def _effective_strategy_ids(self, strategy_ids: tuple[str, ...]) -> tuple[str, ...]:
        if self.strategy_settings_repo is None:
            return strategy_ids
        return await self.strategy_settings_repo.filter_enabled(strategy_ids)

    async def _fetch_market_context(self, symbol: str, interval: str) -> tuple:
        if self.derivatives_repo is None or self.liquidation_repo is None:
            return None, None
        derivatives_snapshot = await fetch_derivatives_snapshot(
            self.derivatives_repo, self.candle_repo, symbol, interval,
            self.risk_settings.funding_zscore_lookback, self.risk_settings.derivatives_hist_limit,
        )
        liquidation_snapshot = await fetch_liquidation_snapshot(
            self.liquidation_repo, symbol,
            self.risk_settings.liquidation_window_seconds, self.risk_settings.liquidation_baseline_buckets,
        )
        return derivatives_snapshot, liquidation_snapshot

    async def sync_equity(self, account_id: str) -> None:
        """Call once per poll cycle (not once per symbol - it's the same
        account-wide number regardless of which symbol triggered the call),
        BEFORE evaluating any entry for this cycle - Fase 17b's real-money
        position-sizing safety fix. See
        BingXExecutionProvider.get_equity/RiskRepository.
        sync_equity_from_exchange for why locally-tracked equity drifts
        from the exchange's real balance and why that matters far more once
        real money (not testnet/VST) is involved. Scaled down by this
        engine's capital_allocation_pct (Fase 17g) when configured - the
        real exchange balance is shared with a sibling engine (e.g.
        Momentum BingX), so sizing must operate on this engine's own slice,
        not the whole account."""
        real_equity = await self.execution.get_equity()
        if self.capital_allocation_repo is not None and self.capital_allocation_key is not None:
            allocation_pct = await self.capital_allocation_repo.get_allocation(self.capital_allocation_key)
            real_equity *= allocation_pct
        await self.risk_repo.sync_equity_from_exchange(account_id, real_equity)

    async def _update_exposure(self, account_id: str, interval: str) -> None:
        """Recomputes open_positions_count AND correlated_exposure_pct
        (PortfolioCorrelationEngine, Fase 17) every time a position opens or
        closes - the two RiskEngine gates (`MAX_POSITIONS`,
        `CORRELATED_EXPOSURE`) both depend on this staying current."""
        open_symbols = await self.shadow_repo.get_open_symbols(account_id)
        correlation = await compute_account_correlated_exposure_from_candles(self.candle_repo, open_symbols, interval)
        await self.risk_repo.set_exposure(account_id, len(open_symbols), correlation.correlated_exposure_pct)

    async def run_once(self, config: ShadowTradingConfig, symbol_rules) -> dict:
        account_id = config.account_id
        account = await self.risk_repo.get_account_state(account_id)
        if account is None:
            raise ValueError(f"shadow account {account_id!r} not initialized - call initialize_account_state first")

        open_position = await self.shadow_repo.get_open_position(account_id, config.symbol)
        if open_position is not None:
            return await self._handle_open_position(config, open_position, account_id)

        return await self._handle_no_open_position(config, account, symbol_rules, account_id)

    async def _handle_open_position(self, config: ShadowTradingConfig, position: ShadowPosition, account_id: str) -> dict:
        live_position = await self.execution.get_position(config.symbol)
        if live_position.position_amt != 0:
            return {"action": "POSITION_STILL_OPEN", "side": position.side, "entry_price": position.entry_price}

        stop_status = await self.execution.get_order_status(config.symbol, position.stop_order_id)
        tp_status = await self.execution.get_order_status(config.symbol, position.take_profit_order_id)

        if stop_status.status == "FILLED":
            filled_order, leftover_order_id, exit_reason = stop_status, position.take_profit_order_id, "STOP"
        elif tp_status.status == "FILLED":
            filled_order, leftover_order_id, exit_reason = tp_status, position.stop_order_id, "TAKE_PROFIT"
        else:
            # The exchange says flat, but neither bracket leg shows FILLED -
            # closed some other way (manual intervention, liquidation, ADL).
            # Never fabricate a price for this - record it as a local
            # inconsistency, close the local record so it stops blocking
            # new entries, and surface it loudly for manual review.
            await self.shadow_repo.close_position(account_id, config.symbol)
            await self._update_exposure(account_id, config.interval)
            return {
                "action": "RECONCILIATION_FAILED", "stop_status": stop_status.status, "tp_status": tp_status.status,
                "message": "position is flat on the exchange but neither bracket leg shows FILLED - manual review needed",
            }

        await self.execution.cancel_leftover_order(config.symbol, leftover_order_id)

        # slippage_pct=0.0: the real fill price already reflects whatever
        # slippage actually happened - simulating additional slippage on
        # top of a REAL exchange fill would double-count it.
        outcome = close_position(
            OpenPosition(side=position.side, entry_time=position.entry_time, entry_price=position.entry_price,
                         stop_price=position.stop_price, take_profit_price=position.take_profit_price,
                         quantity=position.quantity, risk_amount=position.risk_amount,
                         confluence_score=position.confluence_score, reasons=position.reasons),
            raw_exit_price=filled_order.avg_price, exit_reason=exit_reason,
            fees_pct=config.fees_pct, slippage_pct=0.0,
        )
        exit_time = datetime.fromtimestamp(filled_order.update_time_ms / 1000, tz=UTC)
        trade = ShadowTrade(
            account_id=account_id, symbol=config.symbol, side=position.side,
            entry_time=position.entry_time, entry_price=position.entry_price,
            exit_time=exit_time, exit_price=outcome.exit_price, exit_reason=outcome.exit_reason,
            quantity=position.quantity, gross_pnl=outcome.gross_pnl, fees_paid=outcome.fees_paid,
            net_pnl=outcome.net_pnl, r_multiple=outcome.r_multiple,
            confluence_score=position.confluence_score, reasons=position.reasons,
        )
        await self.shadow_repo.record_trade(trade)
        await self.shadow_repo.close_position(account_id, config.symbol)
        await self._update_exposure(account_id, config.interval)
        account = await self.risk_repo.record_trade_outcome(account_id, trade.net_pnl)
        kill_state = await self.kill_switch_repo.check_and_maybe_trigger(
            account_id, account.equity, account.peak_equity, account.consecutive_losses, self.risk_settings,
        )
        return {
            "action": "POSITION_CLOSED", "trade": trade,
            "kill_switch_triggered": kill_state.is_triggered, "kill_switch_reasons": kill_state.reasons,
        }

    async def _handle_no_open_position(self, config: ShadowTradingConfig, account, symbol_rules, account_id: str) -> dict:
        df = await self.candle_repo.fetch_ohlcv(
            config.symbol, config.interval, limit=config.candle_limit, closed_only=True,
        )
        if len(df) < config.warmup_bars + 1:
            return {"action": "NOT_ENOUGH_HISTORY", "candles": len(df), "needed": config.warmup_bars + 1}

        latest_closed = df.iloc[-1]
        cursor = await self.shadow_repo.get_cursor(account_id, config.symbol, config.interval)
        if cursor is not None and latest_closed["close_time"] <= cursor:
            return {"action": "NO_NEW_CANDLE", "latest_close_time": latest_closed["close_time"]}

        kill_state = await self.kill_switch_repo.get_state(account_id)
        if kill_state.is_triggered:
            await self.shadow_repo.set_cursor(account_id, config.symbol, config.interval, latest_closed["close_time"])
            return {"action": "KILL_SWITCH_BLOCKED", "reasons": kill_state.reasons}

        snapshot = compute_snapshot(config.symbol, config.interval, df)
        derivatives_snapshot, liquidation_snapshot = await self._fetch_market_context(config.symbol, config.interval)
        strategy_ids = await self._effective_strategy_ids(config.strategy_ids)
        signals = evaluate_all(
            snapshot, strategy_ids,
            derivatives_snapshot=derivatives_snapshot, liquidation_snapshot=liquidation_snapshot,
        )
        confluence = combine_signals(signals, config.strategy_weights, config.confluence_threshold)
        await self.shadow_repo.set_cursor(account_id, config.symbol, config.interval, latest_closed["close_time"])

        if confluence.decision == "NO_TRADE":
            return {"action": "NO_SIGNAL", "confluence_score": confluence.confluence_score}

        side = confluence.decision
        # Stop/TP are sized off the last closed candle's close, BEFORE the
        # real entry fill price is known - Binance requires the protective
        # orders' trigger prices submitted right after the entry, and the
        # real fill (assumed close to this reference price for liquid
        # symbols) isn't known until the entry order's response comes back.
        # A documented simplification (spec rule 151), not a claim that the
        # stop distance is measured from the exact real entry.
        reference_price = float(latest_closed["close"])
        raw_stop_price = compute_stop(reference_price, snapshot.atr_14, side, config.stop_atr_multiple)
        if raw_stop_price is None:
            return {"action": "NO_STOP_AVAILABLE", "confluence_score": confluence.confluence_score}
        raw_take_profit_price = compute_take_profit(reference_price, raw_stop_price, side, config.take_profit_r_multiple)
        # Binance rejects a price with more decimal places than the symbol's
        # tick_size allows (-1111 "Precision is over the maximum defined for
        # this asset") - caught live on ETHUSDT (ATR math produces an
        # arbitrary-precision float, tick_size doesn't). Rounding direction
        # isn't optimized (always down) - the one-tick difference is
        # immaterial next to an ATR-wide stop distance; getting a valid,
        # acceptable price matters here, not which side of the tick it lands on.
        stop_price = round_down_to_step(raw_stop_price, symbol_rules.tick_size)
        take_profit_price = round_down_to_step(raw_take_profit_price, symbol_rules.tick_size)

        proposal = TradeProposal(
            symbol=config.symbol, side=side, entry_price=reference_price, stop_price=stop_price,
            take_profit_price=take_profit_price, leverage=config.leverage,
        )
        risk_decision = self.risk_engine.evaluate(proposal, account, symbol_rules)
        await self.risk_repo.insert_risk_event(account_id, config.symbol, side, risk_decision)
        if risk_decision.decision != "PASS":
            return {"action": "ENTRY_BLOCKED", "reasons": risk_decision.reasons, "confluence_score": confluence.confluence_score}

        quantity = risk_decision.position_size.quantity
        try:
            brackets = await self.execution.open_bracket_position(
                config.symbol, side, quantity, stop_price, take_profit_price, config.leverage,
            )
        except BracketOpenError as exc:
            return {
                "action": "BRACKET_FAILED", "flattened": exc.flattened, "error": str(exc),
                "confluence_score": confluence.confluence_score,
            }

        reasons = [r for sig in signals for r in sig.reasons if sig.signal != "NO_TRADE"]
        entry_price = brackets.entry.avg_price or apply_slippage(reference_price, side, is_entry=True, slippage_pct=0.0)
        position = ShadowPosition(
            side=side, entry_time=latest_closed["close_time"], entry_price=entry_price,
            quantity=brackets.entry.executed_qty or quantity,
            stop_order_id=brackets.stop.order_id, take_profit_order_id=brackets.take_profit.order_id,
            stop_price=stop_price, take_profit_price=take_profit_price,
            risk_amount=risk_decision.position_size.risk_amount,
            confluence_score=confluence.confluence_score, reasons=reasons,
        )
        await self.shadow_repo.open_position(account_id, config.symbol, position)
        await self._update_exposure(account_id, config.interval)
        return {
            "action": "ENTRY_OPENED", "side": side, "entry_price": entry_price,
            "quantity": position.quantity, "confluence_score": confluence.confluence_score,
        }
