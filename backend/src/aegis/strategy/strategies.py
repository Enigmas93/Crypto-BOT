"""Strategy Engine - spec sections 47/48/49/51.

Every strategy is a pure function - no I/O, no lookahead by construction
(every input is already a bounded snapshot, whatever the caller sliced it
to be). TREND_PULLBACK/BREAKOUT/MEAN_REVERSION take only a
`TechnicalSnapshot` (candles); EVENT_REACTION additionally takes an
`AssetNewsStatus | None` (spec section 41's own NEWS_CONFLICT/
WAIT_FOR_CONFIRMATION/CONFIRMED verdict) - still a pure function, still no
I/O, the caller is responsible for fetching that status.

LIQUIDATION_SQUEEZE remains unimplemented - checked live 2026-09-23 after
the Liquidation Engine had been running continuously for months: the
`liquidations` table held zero genuine events for any of the real 7 traded
symbols (every row that existed was leftover integration-test debris, now
cleaned up and prevented from recurring - see tests/conftest.py). The
engine, its WebSocket subscription and its filtering are all confirmed
working correctly; Binance Futures TESTNET simply does not appear to
generate meaningful forced-liquidation volume the way mainnet does (far
less real leverage abuse against fake money). Building a strategy against
a feed that has never once fired for these symbols would mean shipping
untested logic dressed up as tested - it stays unimplemented until there
is a real signal to build and validate it against, which likely means
mainnet.

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

from aegis.news.conflict import AssetNewsStatus
from aegis.strategy.models import StrategySignal
from aegis.technical.service import TechnicalSnapshot

STRATEGY_TREND_PULLBACK = "TREND_PULLBACK"
STRATEGY_BREAKOUT = "BREAKOUT"
STRATEGY_MEAN_REVERSION = "MEAN_REVERSION"
STRATEGY_EVENT_REACTION = "EVENT_REACTION"

# EVENT_REACTION is intentionally excluded - see module docstring (no
# point-in-time news history yet, so it can't be backtested honestly and
# must not silently activate in any live engine until it can be).
ALL_STRATEGY_IDS = (STRATEGY_TREND_PULLBACK, STRATEGY_BREAKOUT, STRATEGY_MEAN_REVERSION)


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
) -> list[StrategySignal]:
    signals = []
    for sid in strategy_ids:
        if sid == STRATEGY_EVENT_REACTION:
            signals.append(evaluate_event_reaction(snapshot, news_status))
        else:
            signals.append(STRATEGY_FUNCTIONS[sid](snapshot))
    return signals
