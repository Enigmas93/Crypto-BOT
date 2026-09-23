import pytest

from aegis.providers.binance.models import SymbolRules
from aegis.risk.service import AccountState, RiskEngine, TradeProposal


class _Settings:
    risk_per_trade = 0.005
    max_daily_loss = 0.02
    max_drawdown = 0.10
    drawdown_caution_pct = 0.05
    drawdown_reduced_risk_pct = 0.075
    risk_reduction_factor_caution = 0.75
    risk_reduction_factor_reduced = 0.50
    risk_reduction_factor_loss_streak = 0.50
    loss_streak_cooldown_threshold = 3
    loss_streak_reduce_risk_threshold = 5
    loss_streak_halt_threshold = 8
    max_leverage = 5
    min_r_multiple = 1.5
    max_open_positions = 10
    max_correlated_exposure_pct = 0.30
    maintenance_margin_rate_estimate = 0.004
    liquidation_safety_margin = 0.75


def _rules() -> SymbolRules:
    return SymbolRules(symbol="BTCUSDT", status="TRADING", price_precision=2, quantity_precision=3,
                        tick_size=0.1, step_size=0.001, min_notional=5.0)


def _healthy_account() -> AccountState:
    return AccountState(equity=1000.0, peak_equity=1000.0, daily_starting_equity=1000.0,
                         daily_realized_pnl=0.0, consecutive_losses=0, open_positions_count=0)


def _proposal(**overrides) -> TradeProposal:
    base = dict(symbol="BTCUSDT", side="LONG", entry_price=50000.0, stop_price=49000.0,
                take_profit_price=53000.0, leverage=3)
    base.update(overrides)
    return TradeProposal(**base)


def test_healthy_trade_passes():
    engine = RiskEngine(_Settings())
    decision = engine.evaluate(_proposal(), _healthy_account(), _rules())
    assert decision.decision == "PASS"
    assert decision.reasons == []
    assert decision.position_size.status == "OK"
    assert decision.position_size.quantity > 0


def test_missing_stop_blocks():
    engine = RiskEngine(_Settings())
    decision = engine.evaluate(_proposal(stop_price=50000.0), _healthy_account(), _rules())
    assert decision.decision == "BLOCK"
    assert "NO_STOP" in decision.reasons


def test_drawdown_halted_blocks_new_trades():
    engine = RiskEngine(_Settings())
    account = AccountState(equity=880.0, peak_equity=1000.0, daily_starting_equity=1000.0,
                            daily_realized_pnl=0.0, consecutive_losses=0, open_positions_count=0)
    decision = engine.evaluate(_proposal(), account, _rules())
    assert decision.decision == "BLOCK"
    assert "DRAWDOWN_LIMIT" in decision.reasons
    assert decision.drawdown_state == "HALTED"


def test_drawdown_reduced_risk_shrinks_position_but_still_passes():
    engine = RiskEngine(_Settings())
    # 8% drawdown -> REDUCED_RISK (>=7.5%, <10% halted)
    account = AccountState(equity=920.0, peak_equity=1000.0, daily_starting_equity=920.0,
                            daily_realized_pnl=0.0, consecutive_losses=0, open_positions_count=0)
    decision = engine.evaluate(_proposal(), account, _rules())
    assert decision.decision == "PASS"
    assert decision.drawdown_state == "REDUCED_RISK"
    assert decision.effective_risk_per_trade == pytest.approx(0.005 * 0.50)


def test_daily_loss_limit_blocks():
    engine = RiskEngine(_Settings())
    account = AccountState(equity=980.0, peak_equity=1000.0, daily_starting_equity=1000.0,
                            daily_realized_pnl=-25.0, consecutive_losses=1, open_positions_count=0)
    decision = engine.evaluate(_proposal(), account, _rules())
    assert decision.decision == "BLOCK"
    assert "DAILY_LOSS_LIMIT" in decision.reasons


def test_loss_streak_halt_blocks():
    engine = RiskEngine(_Settings())
    account = AccountState(equity=1000.0, peak_equity=1000.0, daily_starting_equity=1000.0,
                            daily_realized_pnl=0.0, consecutive_losses=8, open_positions_count=0)
    decision = engine.evaluate(_proposal(), account, _rules())
    assert decision.decision == "BLOCK"
    assert "LOSS_STREAK_HALT" in decision.reasons


def test_loss_streak_reduce_risk_shrinks_position_but_still_passes():
    engine = RiskEngine(_Settings())
    account = AccountState(equity=1000.0, peak_equity=1000.0, daily_starting_equity=1000.0,
                            daily_realized_pnl=0.0, consecutive_losses=5, open_positions_count=0)
    decision = engine.evaluate(_proposal(), account, _rules())
    assert decision.decision == "PASS"
    assert decision.loss_streak_action == "REDUCE_RISK"
    assert decision.effective_risk_per_trade == pytest.approx(0.005 * 0.50)


def test_reduced_risk_and_loss_streak_take_the_more_conservative_factor():
    engine = RiskEngine(_Settings())
    # both REDUCED_RISK (0.50) and COOLDOWN (no reduction) apply - min factor wins, but
    # cooldown alone doesn't reduce, so this should equal just the drawdown factor
    account = AccountState(equity=920.0, peak_equity=1000.0, daily_starting_equity=920.0,
                            daily_realized_pnl=0.0, consecutive_losses=3, open_positions_count=0)
    decision = engine.evaluate(_proposal(), account, _rules())
    assert decision.effective_risk_per_trade == pytest.approx(0.005 * 0.50)


def test_max_open_positions_blocks():
    engine = RiskEngine(_Settings())
    account = AccountState(equity=1000.0, peak_equity=1000.0, daily_starting_equity=1000.0,
                            daily_realized_pnl=0.0, consecutive_losses=0, open_positions_count=10)
    decision = engine.evaluate(_proposal(), account, _rules())
    assert decision.decision == "BLOCK"
    assert "MAX_POSITIONS" in decision.reasons


def test_correlated_exposure_blocks():
    engine = RiskEngine(_Settings())
    account = AccountState(equity=1000.0, peak_equity=1000.0, daily_starting_equity=1000.0,
                            daily_realized_pnl=0.0, consecutive_losses=0, open_positions_count=1,
                            correlated_exposure_pct=0.35)
    decision = engine.evaluate(_proposal(), account, _rules())
    assert decision.decision == "BLOCK"
    assert "CORRELATED_EXPOSURE" in decision.reasons


def test_leverage_above_max_blocks():
    engine = RiskEngine(_Settings())
    decision = engine.evaluate(_proposal(leverage=50), _healthy_account(), _rules())
    assert decision.decision == "BLOCK"
    assert "LEVERAGE_TOO_HIGH" in decision.reasons


def test_bad_risk_reward_blocks():
    engine = RiskEngine(_Settings())
    # entry=50000, stop=49000 (risk=1000), tp=50500 (reward=500) -> well under min_r_multiple=1.5
    decision = engine.evaluate(_proposal(take_profit_price=50500.0), _healthy_account(), _rules())
    assert decision.decision == "BLOCK"
    assert "BAD_RISK_REWARD" in decision.reasons


def test_no_take_profit_skips_risk_reward_check():
    engine = RiskEngine(_Settings())
    decision = engine.evaluate(_proposal(take_profit_price=None), _healthy_account(), _rules())
    assert "BAD_RISK_REWARD" not in decision.reasons
    assert decision.net_r_multiple is None


def test_liquidation_too_close_blocks():
    engine = RiskEngine(_Settings())
    # wide stop (10%) at high leverage (20x) - liquidation would trigger before the stop
    decision = engine.evaluate(
        _proposal(stop_price=45000.0, take_profit_price=65000.0, leverage=20), _healthy_account(), _rules(),
    )
    assert decision.decision == "BLOCK"
    assert "LIQUIDATION_TOO_CLOSE" in decision.reasons


def test_multiple_block_reasons_all_reported_together():
    engine = RiskEngine(_Settings())
    account = AccountState(equity=880.0, peak_equity=1000.0, daily_starting_equity=1000.0,
                            daily_realized_pnl=-25.0, consecutive_losses=8, open_positions_count=0)
    decision = engine.evaluate(_proposal(leverage=50), account, _rules())
    assert decision.decision == "BLOCK"
    assert {"DRAWDOWN_LIMIT", "DAILY_LOSS_LIMIT", "LOSS_STREAK_HALT", "LEVERAGE_TOO_HIGH"} <= set(decision.reasons)


def test_below_min_notional_surfaces_as_a_block_reason():
    engine = RiskEngine(_Settings())
    # equity=18 -> risk_amount=0.09; stop_distance=1000 -> raw_qty=0.00009.
    # A fine step keeps that nonzero (not ZERO_QUANTITY) but its notional
    # (0.00009 * 50000 = 4.5) still falls under min_notional=5.0.
    tiny_account = AccountState(equity=18.0, peak_equity=18.0, daily_starting_equity=18.0,
                                 daily_realized_pnl=0.0, consecutive_losses=0, open_positions_count=0)
    fine_step_rules = SymbolRules(symbol="BTCUSDT", status="TRADING", price_precision=2,
                                   quantity_precision=5, tick_size=0.1, step_size=0.00001, min_notional=5.0)
    decision = engine.evaluate(_proposal(), tiny_account, fine_step_rules)
    assert decision.decision == "BLOCK"
    assert "BELOW_MIN_NOTIONAL" in decision.reasons
    assert decision.position_size.quantity > 0
