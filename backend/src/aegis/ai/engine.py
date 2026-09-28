"""AiEngine - runs every AI task on its own schedule, fully decoupled from
order placement (engines never await anything here).

Tasks: classify fresh news, write an hourly market brief per traded symbol,
review each closed trade, and run the listing radar. Each task catches its
own failures so one flaky model or feed never stalls the others.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from aegis.ai.analysts import (BRIEF_SYSTEM, NEWS_SYSTEM, REVIEW_SYSTEM, build_brief_prompt, build_news_prompt,
                               build_review_prompt, parse_market_brief, parse_news_analysis, parse_trade_review)
from aegis.logging_utils import get_logger, log_event
from aegis.regime.service import classify_regime
from aegis.strategy.confluence import combine_signals
from aegis.strategy.strategies import evaluate_all
from aegis.technical.service import compute_snapshot

_LOG = get_logger("ai.engine")


def _fmt(v, nd=2):
    return None if v is None else round(float(v), nd)


def build_brief_snapshot(tech, regime, derivatives, news_status, pulse: dict | None, confluence) -> dict:
    """Plain, human-readable facts only - the model is told never to invent
    numbers that are not here."""
    snap = {
        "timeframe": tech.interval,
        "close": _fmt(tech.close, 6),
        "trend_structure": tech.market_structure_trend,
        "regime": regime.trend_regime,
        "volatility_regime": regime.volatility_regime,
        "ADX14": _fmt(tech.adx_14, 1),
        "RSI14": _fmt(tech.rsi_14, 1),
        "distance_from_EMA200_pct": _fmt(tech.distance_from_ema200_pct),
        "distance_from_VWAP_pct": _fmt(tech.distance_from_vwap_pct),
        "ROC12_pct": _fmt(tech.roc_12),
        "volume_zscore20": _fmt(tech.volume_zscore_20),
        "EMA_alignment": ("bullish (close>EMA50>EMA200)" if tech.close and tech.ema_50 and tech.ema_200
                          and tech.close > tech.ema_50 > tech.ema_200 else
                          "bearish (close<EMA50<EMA200)" if tech.close and tech.ema_50 and tech.ema_200
                          and tech.close < tech.ema_50 < tech.ema_200 else "mixed"),
        "system_confluence_decision": confluence.decision,
        "system_confluence_net_score": _fmt(confluence.net_score, 1),
        "system_active_signals": ", ".join(f"{s.strategy_id}={s.signal}" for s in confluence.strategy_signals
                                           if s.signal != "NO_TRADE") or "none",
    }
    if derivatives is not None:
        snap.update({
            "funding_rate": _fmt(derivatives.funding_rate, 6),
            "funding_zscore": _fmt(derivatives.funding_zscore),
            "open_interest_change_pct": _fmt(derivatives.oi_change_pct),
            "price_vs_open_interest": derivatives.price_oi_pattern,
            "global_long_short_ratio": _fmt(derivatives.global_long_short_ratio),
        })
    if news_status is not None:
        snap["news_verdict_24h"] = f"{news_status.status} ({news_status.dominant_sentiment}, " \
                                   f"{news_status.distinct_sources} sources)"
    if pulse:
        snap["ai_news_score_24h"] = f"{pulse['score']} over {pulse['items']} items (max magnitude {pulse['max_magnitude']})"
    return snap


class AiEngine:
    def __init__(self, client, ai_repo, candle_repo=None, news_repo=None, derivatives_repo=None,
                 settings=None, radar=None, bingx_rest=None, notifier=None) -> None:
        self.client = client
        self.ai_repo = ai_repo
        self.candle_repo = candle_repo
        self.news_repo = news_repo
        self.derivatives_repo = derivatives_repo
        self.settings = settings
        self.radar = radar
        self.bingx_rest = bingx_rest
        self.notifier = notifier

    # -- news -------------------------------------------------------------
    async def classify_pending_news(self, limit: int = 6) -> int:
        done = 0
        for item in await self.ai_repo.fetch_news_pending_analysis(limit=limit):
            result = await self.client.complete_json(
                NEWS_SYSTEM, build_news_prompt(item["title"], item["summary"] or "", item["source_name"]),
                max_tokens=500,
            )
            analysis = parse_news_analysis(result.data if result else None)
            if analysis is None:
                continue
            await self.ai_repo.upsert_news_analysis(item["source_id"], item["guid"], result.model, analysis)
            done += 1
        return done

    # -- market briefs ------------------------------------------------------
    async def write_briefs(self, symbols: list[str], interval: str = "1h", every_minutes: int = 60) -> int:
        pulse = {row["asset"]: row for row in await self.ai_repo.ai_news_pulse(24)}
        written = 0
        for symbol in symbols:
            last = await self.ai_repo.last_insight_time("MARKET_BRIEF", symbol)
            if last is not None and datetime.now(UTC) - last < timedelta(minutes=every_minutes):
                continue
            df = await self.candle_repo.fetch_ohlcv(symbol, interval, limit=500, closed_only=True)
            if len(df) < 210:
                continue
            tech = compute_snapshot(symbol, interval, df)
            confluence = combine_signals(evaluate_all(tech))
            derivatives = None
            if self.derivatives_repo is not None and self.settings is not None:
                from aegis.strategy.market_context import fetch_derivatives_snapshot
                try:
                    derivatives = await fetch_derivatives_snapshot(
                        self.derivatives_repo, self.candle_repo, symbol, interval,
                        self.settings.funding_zscore_lookback, self.settings.derivatives_hist_limit,
                    )
                except Exception:  # noqa: BLE001 - optional context
                    derivatives = None
            asset = symbol.removesuffix("USDT").removeprefix("1000")
            news_status = await self.news_repo.get_asset_status(asset) if self.news_repo else None
            snapshot = build_brief_snapshot(tech, classify_regime(tech), derivatives, news_status,
                                            pulse.get(asset), confluence)
            result = await self.client.complete_json(BRIEF_SYSTEM, build_brief_prompt(symbol, snapshot), 700)
            brief = parse_market_brief(result.data if result else None)
            if brief is None:
                continue
            await self.ai_repo.insert_insight("MARKET_BRIEF", symbol, result.model, {
                "bias": brief.bias, "conviction": brief.conviction, "risk_level": brief.risk_level,
                "key_points": brief.key_points, "summary_pt": brief.summary_pt, "snapshot": snapshot,
                "system_decision": confluence.decision, "price": tech.close,
            })
            written += 1
        return written

    # -- trade reviews --------------------------------------------------------
    async def review_closed_trades(self, limit: int = 2) -> int:
        done = 0
        for t in await self.ai_repo.fetch_trades_pending_review(limit=limit):
            facts = {
                "engine": t["engine"], "account": t["account_id"], "symbol": t["symbol"], "side": t["side"],
                "entry_time": t["entry_time"], "entry_price": t["entry_price"], "exit_time": t["exit_time"],
                "exit_price": t["exit_price"], "exit_reason": t["exit_reason"],
                "result_R": _fmt(t["r_multiple"]), "net_pnl_usdt": _fmt(t["net_pnl"]),
                "entry_reasons": "; ".join(t["reasons"] or []),
                "max_favorable_excursion_R": _fmt(t["mfe_r"]), "max_adverse_excursion_R": _fmt(t["mae_r"]),
                "confluence_score": _fmt(t["confluence_score"], 1),
            }
            result = await self.client.complete_json(REVIEW_SYSTEM, build_review_prompt(facts), 500)
            review = parse_trade_review(result.data if result else None)
            if review is None:
                continue
            await self.ai_repo.insert_insight("TRADE_REVIEW", f"{t['engine']}:{t['id']}", result.model, {
                "verdict": review.verdict, "lesson_pt": review.lesson_pt, "symbol": t["symbol"],
                "side": t["side"], "r_multiple": t["r_multiple"], "account_id": t["account_id"],
                "exit_reason": t["exit_reason"],
            })
            done += 1
        return done

    # -- listing radar ---------------------------------------------------------
    async def run_listing_radar(self, alert_window_minutes: int = 120) -> int:
        if self.radar is None:
            return 0
        events = await self.radar.fetch_all()
        recent = [e for e in events if datetime.now(UTC) - e.announced_at < timedelta(days=3)]
        if not recent:
            return 0
        tickers = {}
        if self.bingx_rest is not None:
            try:
                tickers = {t.symbol: t for t in await self.bingx_rest.get_24h_tickers()}
            except Exception:  # noqa: BLE001 - radar still records the event without a price
                tickers = {}
        new_count = 0
        for e in recent:
            bingx_symbol = next((s for s in (f"{e.ticker}USDT", f"1000{e.ticker}USDT") if s in tickers), None)
            price = tickers[bingx_symbol].last_price if bingx_symbol else None
            if not await self.ai_repo.insert_listing_event(e.source, e.ticker, e.title, e.announced_at,
                                                           bingx_symbol, price):
                continue
            new_count += 1
            fresh = datetime.now(UTC) - e.announced_at < timedelta(minutes=alert_window_minutes)
            log_event(_LOG, "listing_detected", source=e.source, ticker=e.ticker, bingx=bingx_symbol, fresh=fresh)
            if fresh and self.notifier is not None:
                await self.notifier.send(
                    f"📢 Radar de listagem: {e.source} → {e.ticker}\n{e.title}\n"
                    + (f"No BingX: {bingx_symbol} @ {price}" if bingx_symbol else "Sem perpétuo no BingX")
                    + "\nHistórico: o movimento costuma acontecer antes de um robô por polling conseguir "
                      "entrar - alerta informativo, sem ordem automática."
                )
        return new_count
