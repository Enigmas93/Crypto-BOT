"""Strategy Engine - spec sections 47/48/49/51.

Every strategy is a pure function - no I/O, no lookahead by construction
(every input is already a bounded snapshot, whatever the caller sliced it
to be). TREND_PULLBACK/BREAKOUT/MEAN_REVERSION take only a
`TechnicalSnapshot` (candles); EVENT_REACTION additionally takes an
`AssetNewsStatus | None` (spec section 41's own NEWS_CONFLICT/
WAIT_FOR_CONFIRMATION/CONFIRMED verdict) - still a pure function, still no
I/O, the caller is responsible for fetching that status.

LIQUIDATION_SQUEEZE (`evaluate_liquidation_squeeze`) was held out of
`ALL_STRATEGY_IDS` at first (Fase 17) for a documented reason: after the
Liquidation Engine ran continuously for months, the `liquidations` table
held zero genuine events for any of the real 7 traded symbols on Binance
Futures TESTNET. The user explicitly asked (Fase 17e) to activate it and
confirm it's live anyway, understanding that caveat - it is now in
`ALL_STRATEGY_IDS` and wired with REAL derivatives/liquidation data in
Paper and Shadow Trading (Binance) via `aegis.strategy.market_context`.
It stays functionally inert (always NO_TRADE) for every BingX account
(shadow_bingx/momentum_bingx, demo or live) and for Momentum's dynamic
scanner-picked universe, because neither BingX nor Momentum's arbitrary
candidates have derivatives/liquidation data collected anywhere in this
codebase - a real, disclosed limitation, not a silent gap: it will start
contributing real votes there only once that collection exists.

EVENT_REACTION's own limitation, different in kind: `news_asset_status`
(aegis/db/news_repository.py) is a single upserted row per asset - the
CURRENT verdict, not a time series. That's correct for Shadow/Paper/
Momentum (real-time engines asking "what does the news say right now"),
but the Backtest Engine replays PAST candles - reading "the current status"
while evaluating a historical bar would leak today's information into a
past decision, exactly the lookahead bug this whole codebase is built to
avoid everywhere else. So EVENT_REACTION is implemented and fully unit
tested here (the pure decision logic is correct and verified), but is
DELIBERATELY not wired into BacktestEngine, and DELIBERATELY not added to
`ALL_STRATEGY_IDS` (so it never silently activates in Shadow/Paper/
Momentum either) - it needs a point-in-time news-status history table
before it can be backtested honestly, which needs its own migration and is
left as an explicit next step, not attempted here as a rushed add-on.

Never a single indicator alone: every strategy here requires at least two
independent conditions (trend regime + trigger, or range regime +
extreme, or confirmed news + price already confirming the same direction)
before it will say anything but NO_TRADE (spec section 2).
"""
from __future__ import annotations

from aegis.derivatives.service import DerivativesSnapshot
from aegis.liquidation.service import LiquidationSnapshot
from aegis.news.conflict import AssetNewsStatus
from aegis.strategy.models import StrategySignal
from aegis.technical.service import TechnicalSnapshot

STRATEGY_TREND_PULLBACK = "TREND_PULLBACK"
STRATEGY_BREAKOUT = "BREAKOUT"
STRATEGY_MEAN_REVERSION = "MEAN_REVERSION"
STRATEGY_EVENT_REACTION = "EVENT_REACTION"
STRATEGY_LIQUIDATION_SQUEEZE = "LIQUIDATION_SQUEEZE"

# EVENT_REACTION is intentionally excluded - see module docstring (no
# point-in-time news history yet, so it can't be backtested honestly and
# must not silently activate in any live engine until it can be).
ALL_STRATEGY_IDS = (
    STRATEGY_TREND_PULLBACK, STRATEGY_BREAKOUT, STRATEGY_MEAN_REVERSION, STRATEGY_LIQUIDATION_SQUEEZE,
)


def _no_trade(strategy_id: str, snapshot: TechnicalSnapshot, reasons: list[str]) -> StrategySignal:
    return StrategySignal(strategy_id, snapshot.symbol, snapshot.interval, snapshot.as_of, "NO_TRADE", 0.0, reasons)


def evaluate_trend_pullback(
    snapshot: TechnicalSnapshot, adx_threshold: float = 20.0,
    rsi_pullback_low: float = 40.0, rsi_pullback_high: float = 60.0,
) -> StrategySignal:
    """spec section 48: higher-timeframe trend + EMA alignment + ADX +
    pullback + market structure. "Pullback" is approximated as RSI cooling
    into a neutral band (40-60) - neither overbought continuation nor a
    breakdown - while the trend itself stays intact (EMA order, ADX,
    structure)."""
    required = (snapshot.close, snapshot.ema_50, snapshot.ema_200, snapshot.adx_14, snapshot.rsi_14)
    if snapshot.as_of is None or any(v is None for v in required):
        return _no_trade(STRATEGY_TREND_PULLBACK, snapshot, ["INSUFFICIENT_DATA"])

    trending = snapshot.adx_14 >= adx_threshold
    pullback = rsi_pullback_low <= snapshot.rsi_14 <= rsi_pullback_high
    bullish_alignment = snapshot.close > snapshot.ema_50 > snapshot.ema_200
    bearish_alignment = snapshot.close < snapshot.ema_50 < snapshot.ema_200

    strength = min(100.0, max(0.0, snapshot.adx_14 * 2.0))

    if bullish_alignment and trending and pullback and snapshot.market_structure_trend == "UPTREND":
        reasons = [
            f"bullish EMA alignment (close>{snapshot.ema_50:.2f}>{snapshot.ema_200:.2f})",
            f"ADX {snapshot.adx_14:.1f} >= {adx_threshold}", f"RSI {snapshot.rsi_14:.1f} pullback band",
            "structure=UPTREND",
        ]
        return StrategySignal(STRATEGY_TREND_PULLBACK, snapshot.symbol, snapshot.interval,
                               snapshot.as_of, "LONG", strength, reasons)

    if bearish_alignment and trending and pullback and snapshot.market_structure_trend == "DOWNTREND":
        reasons = [
            f"bearish EMA alignment (close<{snapshot.ema_50:.2f}<{snapshot.ema_200:.2f})",
            f"ADX {snapshot.adx_14:.1f} >= {adx_threshold}", f"RSI {snapshot.rsi_14:.1f} pullback band",
            "structure=DOWNTREND",
        ]
        return StrategySignal(STRATEGY_TREND_PULLBACK, snapshot.symbol, snapshot.interval,
                               snapshot.as_of, "SHORT", strength, reasons)

    return _no_trade(STRATEGY_TREND_PULLBACK, snapshot, ["no trend+alignment+pullback+structure confluence"])


def evaluate_breakout(snapshot: TechnicalSnapshot, volume_zscore_threshold: float = 1.0) -> StrategySignal:
    """spec section 49: structural breakout/breakdown (from
    `market_structure`, already anti-lookahead by its fractal design)
    confirmed by volume expansion. Never a breakout on thin volume."""
    if snapshot.as_of is None or snapshot.volume_zscore_20 is None:
        return _no_trade(STRATEGY_BREAKOUT, snapshot, ["INSUFFICIENT_DATA"])

    volume_confirmed = snapshot.volume_zscore_20 >= volume_zscore_threshold
    strength = min(100.0, max(0.0, 50.0 + snapshot.volume_zscore_20 * 15.0))

    if snapshot.breakout and volume_confirmed:
        reasons = [f"breakout above last confirmed swing high ({snapshot.last_swing_high})",
                   f"volume z-score {snapshot.volume_zscore_20:.2f} >= {volume_zscore_threshold}"]
        return StrategySignal(STRATEGY_BREAKOUT, snapshot.symbol, snapshot.interval,
                               snapshot.as_of, "LONG", strength, reasons)

    if snapshot.breakdown and volume_confirmed:
        reasons = [f"breakdown below last confirmed swing low ({snapshot.last_swing_low})",
                   f"volume z-score {snapshot.volume_zscore_20:.2f} >= {volume_zscore_threshold}"]
        return StrategySignal(STRATEGY_BREAKOUT, snapshot.symbol, snapshot.interval,
                               snapshot.as_of, "SHORT", strength, reasons)

    reasons = ["no breakout/breakdown"] if not (snapshot.breakout or snapshot.breakdown) else ["volume not confirmed"]
    return _no_trade(STRATEGY_BREAKOUT, snapshot, reasons)


def evaluate_mean_reversion(
    snapshot: TechnicalSnapshot, rsi_overbought: float = 70.0, rsi_oversold: float = 30.0,
    adx_range_max: float = 20.0, vwap_distance_threshold: float = 2.0,
) -> StrategySignal:
    """spec section 51: RSI extreme is never sufficient alone - requires a
    ranging regime (low ADX, i.e. not a trending market where "extreme"
    RSI just means a strong trend) AND meaningful distance from VWAP."""
    required = (snapshot.close, snapshot.rsi_14, snapshot.adx_14, snapshot.distance_from_vwap_pct)
    if snapshot.as_of is None or any(v is None for v in required):
        return _no_trade(STRATEGY_MEAN_REVERSION, snapshot, ["INSUFFICIENT_DATA"])

    ranging = snapshot.adx_14 <= adx_range_max
    overbought = snapshot.rsi_14 >= rsi_overbought and snapshot.distance_from_vwap_pct >= vwap_distance_threshold
    oversold = snapshot.rsi_14 <= rsi_oversold and snapshot.distance_from_vwap_pct <= -vwap_distance_threshold

    strength = min(100.0, max(0.0, abs(snapshot.distance_from_vwap_pct) * 10.0))

    if ranging and overbought:
        reasons = [f"RSI {snapshot.rsi_14:.1f} >= {rsi_overbought} (overbought)",
                   f"{snapshot.distance_from_vwap_pct:.2f}% above VWAP", f"ADX {snapshot.adx_14:.1f} <= {adx_range_max} (ranging)"]
        return StrategySignal(STRATEGY_MEAN_REVERSION, snapshot.symbol, snapshot.interval,
                               snapshot.as_of, "SHORT", strength, reasons)

    if ranging and oversold:
        reasons = [f"RSI {snapshot.rsi_14:.1f} <= {rsi_oversold} (oversold)",
                   f"{snapshot.distance_from_vwap_pct:.2f}% below VWAP", f"ADX {snapshot.adx_14:.1f} <= {adx_range_max} (ranging)"]
        return StrategySignal(STRATEGY_MEAN_REVERSION, snapshot.symbol, snapshot.interval,
                               snapshot.as_of, "LONG", strength, reasons)

    return _no_trade(STRATEGY_MEAN_REVERSION, snapshot, ["no ranging+RSI-extreme+VWAP-distance confluence"])


def evaluate_liquidation_squeeze(
    snapshot: TechnicalSnapshot,
    derivatives_snapshot: DerivativesSnapshot | None,
    liquidation_snapshot: LiquidationSnapshot | None,
    funding_extreme_zscore: float = 2.0,
    squeeze_score_threshold: float = 60.0,
) -> StrategySignal:
    """spec section ~54 (03 - Liquidation / Squeeze): "Funding extremo +
    desequilíbrio de OI + aceleração de liquidações -> SqueezeScore. Nunca
    entra só por funding extremo." Three independent conditions, same
    discipline as every other strategy here:
      1. `funding_zscore` extreme in either direction - which side is
         over-leveraged/crowded (spec's "funding extremo").
      2. `global_long_short_ratio_zscore` (this codebase's OI-side
         imbalance proxy - Binance's OI endpoint has no long/short split,
         unlike the ratio endpoints) extreme in the SAME direction as
         funding - confirms the crowding isn't just a funding-rate blip.
      3. `liquidation_imbalance` (which side is actually getting flushed
         right now) also pointing the same way, AND `squeeze_score`
         (already a 0-100 composite of liquidation notional z-score +
         imbalance + acceleration - aegis.liquidation.service) above
         threshold - confirms a real, currently-accelerating cascade, not
         just crowded positioning that hasn't broken yet.

    Direction is a documented HYPOTHESIS, explicitly not yet validated
    (spec section 54's own standard - weights/rules earn their place via
    backtest/walk-forward, never assumed): trades WITH the liquidation
    cascade (momentum, not mean-reversion) - extreme positive funding means
    crowded longs; when the same-signed liquidation_imbalance shows longs
    are the ones being flushed, the read is a long-squeeze cascade
    continuing down, not yet exhausted, so SHORT. Mirrored for extreme
    negative funding + short-side liquidation_imbalance -> LONG (short
    squeeze ripping up). `evaluate_mean_reversion` above already owns the
    contrarian "fade the extreme" read for this codebase - this strategy
    is deliberately the other one, and only backtesting will show which
    (if either) actually has an edge.
    """
    if snapshot.as_of is None:
        return _no_trade(STRATEGY_LIQUIDATION_SQUEEZE, snapshot, ["INSUFFICIENT_DATA"])
    if derivatives_snapshot is None or derivatives_snapshot.funding_zscore is None:
        return _no_trade(STRATEGY_LIQUIDATION_SQUEEZE, snapshot, ["no funding rate data"])
    if liquidation_snapshot is None or liquidation_snapshot.squeeze_score is None:
        return _no_trade(STRATEGY_LIQUIDATION_SQUEEZE, snapshot, ["no liquidation data"])

    funding_z = derivatives_snapshot.funding_zscore
    oi_imbalance_z = derivatives_snapshot.global_long_short_ratio_zscore
    liq_imbalance = liquidation_snapshot.liquidation_imbalance
    squeeze_score = liquidation_snapshot.squeeze_score

    if abs(funding_z) < funding_extreme_zscore:
        return _no_trade(STRATEGY_LIQUIDATION_SQUEEZE, snapshot, ["funding rate not extreme"])
    if squeeze_score < squeeze_score_threshold:
        return _no_trade(STRATEGY_LIQUIDATION_SQUEEZE, snapshot, ["squeeze score below threshold"])
    if oi_imbalance_z is None or liq_imbalance is None:
        return _no_trade(STRATEGY_LIQUIDATION_SQUEEZE, snapshot, ["INSUFFICIENT_DATA"])

    strength = min(100.0, max(0.0, squeeze_score))

    # Crowded longs (funding + OI ratio both skewed long) actually flushing
    # (positive liquidation_imbalance = longs are the ones being liquidated).
    if funding_z > 0 and oi_imbalance_z > 0 and liq_imbalance > 0:
        reasons = [
            f"funding z-score {funding_z:.2f} >= {funding_extreme_zscore} (crowded longs)",
            f"long/short ratio z-score {oi_imbalance_z:.2f} confirms long-side OI imbalance",
            f"liquidation imbalance {liq_imbalance:.2f} - longs being flushed",
            f"squeeze score {squeeze_score:.1f} >= {squeeze_score_threshold}",
        ]
        return StrategySignal(STRATEGY_LIQUIDATION_SQUEEZE, snapshot.symbol, snapshot.interval,
                               snapshot.as_of, "SHORT", strength, reasons)

    # Crowded shorts actually flushing (negative liquidation_imbalance =
    # shorts are the ones being liquidated) -> short squeeze ripping up.
    if funding_z < 0 and oi_imbalance_z < 0 and liq_imbalance < 0:
        reasons = [
            f"funding z-score {funding_z:.2f} <= -{funding_extreme_zscore} (crowded shorts)",
            f"long/short ratio z-score {oi_imbalance_z:.2f} confirms short-side OI imbalance",
            f"liquidation imbalance {liq_imbalance:.2f} - shorts being flushed",
            f"squeeze score {squeeze_score:.1f} >= {squeeze_score_threshold}",
        ]
        return StrategySignal(STRATEGY_LIQUIDATION_SQUEEZE, snapshot.symbol, snapshot.interval,
                               snapshot.as_of, "LONG", strength, reasons)

    return _no_trade(STRATEGY_LIQUIDATION_SQUEEZE, snapshot,
                      ["funding/OI-imbalance/liquidation-imbalance directions don't agree"])


def evaluate_event_reaction(
    snapshot: TechnicalSnapshot, news_status: AssetNewsStatus | None, roc_confirmation_threshold: float = 0.0,
) -> StrategySignal:
    """spec section ~50: react to a real, market-confirmed news event, not
    a headline the market hasn't actually moved on yet. Two independent
    conditions, same discipline as every other strategy here:
      1. `news_status.status == "CONFIRMED"` - never NEWS_CONFLICT (spec
         section 41: "não operar imediatamente") and never a single
         unconfirmed source (WAIT_FOR_CONFIRMATION) - only a verdict that
         already survived aegis.news.conflict's own cross-source check.
      2. Price already moving the same direction as the news sentiment
         (`roc_12`) - confirms the market is actually reacting, not just
         that a headline with the right sentiment exists.
    `strength` scales with `distinct_sources` - more independent
    confirming sources is more conviction, not just "confirmed or not".
    """
    if news_status is None or news_status.status != "CONFIRMED" or news_status.dominant_sentiment is None:
        return _no_trade(STRATEGY_EVENT_REACTION, snapshot, ["no confirmed news event for this asset"])
    if snapshot.as_of is None or snapshot.roc_12 is None:
        return _no_trade(STRATEGY_EVENT_REACTION, snapshot, ["INSUFFICIENT_DATA"])

    sentiment = news_status.dominant_sentiment
    strength = min(100.0, news_status.distinct_sources * 25.0)

    if sentiment == "positive" and snapshot.roc_12 > roc_confirmation_threshold:
        reasons = [
            f"confirmed positive news ({news_status.distinct_sources} independent sources)",
            f"price already reacting (ROC12 {snapshot.roc_12:.2f}% > {roc_confirmation_threshold}%)",
        ]
        return StrategySignal(STRATEGY_EVENT_REACTION, snapshot.symbol, snapshot.interval,
                               snapshot.as_of, "LONG", strength, reasons)

    if sentiment == "negative" and snapshot.roc_12 < -roc_confirmation_threshold:
        reasons = [
            f"confirmed negative news ({news_status.distinct_sources} independent sources)",
            f"price already reacting (ROC12 {snapshot.roc_12:.2f}% < -{roc_confirmation_threshold}%)",
        ]
        return StrategySignal(STRATEGY_EVENT_REACTION, snapshot.symbol, snapshot.interval,
                               snapshot.as_of, "SHORT", strength, reasons)

    return _no_trade(STRATEGY_EVENT_REACTION, snapshot,
                      ["confirmed news exists but price isn't yet confirming the same direction"])


STRATEGY_FUNCTIONS = {
    STRATEGY_TREND_PULLBACK: evaluate_trend_pullback,
    STRATEGY_BREAKOUT: evaluate_breakout,
    STRATEGY_MEAN_REVERSION: evaluate_mean_reversion,
}


def evaluate_all(
    snapshot: TechnicalSnapshot, strategy_ids: tuple[str, ...] = ALL_STRATEGY_IDS,
    news_status: AssetNewsStatus | None = None,
    derivatives_snapshot: DerivativesSnapshot | None = None,
    liquidation_snapshot: LiquidationSnapshot | None = None,
) -> list[StrategySignal]:
    """`derivatives_snapshot`/`liquidation_snapshot` are only consumed by
    STRATEGY_LIQUIDATION_SQUEEZE - never required unless a caller
    explicitly opts that strategy into `strategy_ids` (it is NOT part of
    ALL_STRATEGY_IDS - see module docstring), so Paper/Shadow/Momentum's
    existing calls (which pass neither) are unaffected."""
    signals = []
    for sid in strategy_ids:
        if sid == STRATEGY_EVENT_REACTION:
            signals.append(evaluate_event_reaction(snapshot, news_status))
        elif sid == STRATEGY_LIQUIDATION_SQUEEZE:
            signals.append(evaluate_liquidation_squeeze(snapshot, derivatives_snapshot, liquidation_snapshot))
        else:
            signals.append(STRATEGY_FUNCTIONS[sid](snapshot))
    return signals
