#!/usr/bin/env python
"""Demonstrates and validates the Phase 7 Risk Engine against real data.

Unlike the other `run_*_engine.py` scripts, this isn't a long-running
poller - RiskEngine is a synchronous gate a future Strategy/Execution
Engine calls before every order, not something that polls anything on its
own. There is no live signal stream yet, so this script instead:

  1. Pulls REAL BTCUSDT symbol rules (tick/step/minNotional) from Binance,
     so position sizing is validated against real exchange precision, not
     made-up numbers.
  2. Runs a handful of realistic trade proposals through RiskEngine and
     prints/persists the decisions.
  3. Simulates a losing streak against a real database-backed account
     state to show the drawdown and loss-streak state machines actually
     escalate through NORMAL -> CAUTION -> REDUCED_RISK -> HALTED and
     NONE -> COOLDOWN -> REDUCE_RISK -> HALT.

Usage (from backend/, venv active):
    python scripts/demo_risk_engine.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aegis.config import get_settings  # noqa: E402
from aegis.db.engine import close_pool, create_pool  # noqa: E402
from aegis.db.risk_repository import RiskRepository  # noqa: E402
from aegis.logging_utils import configure_logging, get_logger, log_event  # noqa: E402
from aegis.providers.binance.rest_client import BinanceFuturesRestClient  # noqa: E402
from aegis.risk.service import RiskEngine, TradeProposal

_LOG = get_logger("scripts.demo_risk_engine")
DEMO_ACCOUNT = "demo"


async def _main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)

    rest = BinanceFuturesRestClient(testnet=settings.binance_testnet)
    pool = await create_pool(settings)
    repo = RiskRepository(pool)
    engine = RiskEngine(settings)

    try:
        rules = await rest.get_symbol_rules()
        btc_rules = rules["BTCUSDT"]
        klines = await rest.get_klines("BTCUSDT", "1m", limit=1)
        current_price = klines[-1].close
        log_event(_LOG, "real_market_data", symbol="BTCUSDT", price=current_price,
                   tick_size=btc_rules.tick_size, step_size=btc_rules.step_size,
                   min_notional=btc_rules.min_notional)

        # Start every demo run from a clean $1,000 account so the output is reproducible.
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM risk_account_state WHERE account_id = $1", DEMO_ACCOUNT)
            await conn.execute("DELETE FROM risk_events WHERE account_id = $1", DEMO_ACCOUNT)
        account = await repo.initialize_account_state(DEMO_ACCOUNT, starting_equity=1000.0)

        log_event(_LOG, "scenario_1_healthy_long", message="A normal long, 2R target, real BTC price/precision")
        proposal = TradeProposal(
            symbol="BTCUSDT", side="LONG", entry_price=current_price,
            stop_price=current_price * 0.98, take_profit_price=current_price * 1.04, leverage=3,
        )
        decision = engine.evaluate(proposal, account, btc_rules)
        await repo.insert_risk_event(DEMO_ACCOUNT, proposal.symbol, proposal.side, decision)
        log_event(_LOG, "decision", **_decision_summary(decision))

        log_event(_LOG, "scenario_2_no_stop", message="Same trade but stop == entry - must BLOCK")
        bad_proposal = TradeProposal(symbol="BTCUSDT", side="LONG", entry_price=current_price,
                                      stop_price=current_price, leverage=3)
        decision = engine.evaluate(bad_proposal, account, btc_rules)
        await repo.insert_risk_event(DEMO_ACCOUNT, bad_proposal.symbol, bad_proposal.side, decision)
        log_event(_LOG, "decision", **_decision_summary(decision))

        log_event(_LOG, "scenario_3_over_leveraged", message=f"Leverage above max_leverage={settings.max_leverage}")
        over_leveraged = TradeProposal(symbol="BTCUSDT", side="LONG", entry_price=current_price,
                                        stop_price=current_price * 0.99, leverage=settings.max_leverage + 5)
        decision = engine.evaluate(over_leveraged, account, btc_rules)
        await repo.insert_risk_event(DEMO_ACCOUNT, over_leveraged.symbol, over_leveraged.side, decision)
        log_event(_LOG, "decision", **_decision_summary(decision))

        log_event(
            _LOG, "scenario_4_liquidation_too_close",
            message=(
                f"At the allowed max_leverage={settings.max_leverage}x, the estimated liquidation "
                "cushion is roughly 1/leverage - maintenance_margin - a 20% stop is wide enough to "
                "breach it even though the leverage itself is within the cap"
            ),
        )
        wide_stop_proposal = TradeProposal(symbol="BTCUSDT", side="LONG", entry_price=current_price,
                                            stop_price=current_price * 0.80, leverage=settings.max_leverage)
        decision = engine.evaluate(wide_stop_proposal, account, btc_rules)
        await repo.insert_risk_event(DEMO_ACCOUNT, wide_stop_proposal.symbol, wide_stop_proposal.side, decision)
        log_event(_LOG, "decision", **_decision_summary(decision))

        log_event(
            _LOG, "scenario_5_loss_streak_and_drawdown",
            message=(
                "Simulating one losing trade per day (reset_daily() between each) so the daily-loss "
                "guard doesn't immediately mask the loss-streak and drawdown state machines - shows "
                "both escalating together: NONE->COOLDOWN->REDUCE_RISK->HALT and NORMAL->CAUTION->"
                "REDUCED_RISK->HALTED"
            ),
        )
        for i in range(1, 11):
            await repo.reset_daily(DEMO_ACCOUNT)  # a new trading day begins
            current_equity = (await repo.get_account_state(DEMO_ACCOUNT)).equity
            loss_amount = current_equity * 0.015  # 1.5% of current equity, one loss for "today"
            account = await repo.record_trade_outcome(DEMO_ACCOUNT, -loss_amount)
            decision = engine.evaluate(proposal, account, btc_rules)
            log_event(
                _LOG, "loss_streak_step", day=i, equity=round(account.equity, 2),
                consecutive_losses=account.consecutive_losses, drawdown_state=decision.drawdown_state,
                drawdown_pct=round(decision.drawdown_pct, 4), loss_streak_action=decision.loss_streak_action,
                effective_risk_per_trade=round(decision.effective_risk_per_trade, 5),
                final_decision=decision.decision, reasons=decision.reasons,
            )
    finally:
        await rest.aclose()
        await close_pool(pool)


def _decision_summary(decision) -> dict:
    return {
        "decision": decision.decision,
        "reasons": decision.reasons,
        "drawdown_state": decision.drawdown_state,
        "position_quantity": decision.position_size.quantity,
        "position_notional": round(decision.position_size.notional, 2),
        "net_r_multiple": round(decision.net_r_multiple, 3) if decision.net_r_multiple is not None else None,
        "liquidation_safe": decision.liquidation_check.safe,
    }


if __name__ == "__main__":
    asyncio.run(_main())
