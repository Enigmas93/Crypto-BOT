"""Prompt builders + strict validators for each AI task.

Every model answer passes through a `parse_*` function that clamps and
whitelists every field; anything unusable returns None and the caller keeps
the deterministic fallback (keyword classifier, no brief, no review).
Prompts ask for Portuguese summaries because the dashboard is in PT-BR.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

SENTIMENTS = {"positive", "negative", "neutral"}
EVENT_TYPES = {
    "LISTING", "DELISTING", "HACK_EXPLOIT", "REGULATION", "ETF", "MACRO", "PARTNERSHIP", "TOKEN_UNLOCK",
    "UPGRADE", "LAWSUIT", "WHALE_FLOW", "EARNINGS", "MARKET_COMMENTARY", "OTHER",
}
BIASES = {"LONG", "SHORT", "NEUTRAL"}
RISK_LEVELS = {"LOW", "NORMAL", "HIGH"}
VERDICTS = {"GOOD_TRADE", "BAD_ENTRY", "BAD_EXIT", "UNLUCKY", "CORRECT_LOSS"}
_TICKER = re.compile(r"^[A-Z0-9]{2,12}$")


def _clamp(value, lo: float, hi: float, default: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def _text(value, limit: int) -> str:
    return str(value or "").strip()[:limit]


# -- news ------------------------------------------------------------------
NEWS_SYSTEM = (
    "You are a crypto market news analyst. Judge the likely SHORT-TERM (hours to 2 days) price impact of a "
    "headline on each crypto asset it concerns. Read negations and denials carefully (\"denies hack\" is not a "
    "hack). Opinion pieces and price recaps are MARKET_COMMENTARY with low magnitude. "
    "Answer with ONE JSON object only, no prose."
)


def build_news_prompt(title: str, summary: str, source: str) -> str:
    return (
        f"Source: {source}\nTitle: {title}\nSummary: {summary[:800]}\n\n"
        "Return JSON with keys:\n"
        '"sentiment": "positive"|"negative"|"neutral" (for the main asset),\n'
        '"magnitude": integer 1-5 (1=noise, 5=market-moving),\n'
        '"confidence": number 0-1,\n'
        f'"event_type": one of {sorted(EVENT_TYPES)},\n'
        '"assets": list of affected crypto tickers like ["BTC","ETH"] (empty if none),\n'
        '"summary_pt": one sentence in Brazilian Portuguese explaining the impact (max 200 chars)'
    )


@dataclass(slots=True)
class NewsAnalysis:
    sentiment: str
    magnitude: int
    confidence: float
    event_type: str
    assets: list[str]
    summary_pt: str


def parse_news_analysis(data: dict | None) -> NewsAnalysis | None:
    if not isinstance(data, dict):
        return None
    sentiment = str(data.get("sentiment", "")).lower().strip()
    if sentiment not in SENTIMENTS:
        return None
    event_type = str(data.get("event_type", "OTHER")).upper().strip()
    if event_type not in EVENT_TYPES:
        event_type = "OTHER"
    raw_assets = data.get("assets") or []
    if not isinstance(raw_assets, list):
        raw_assets = []
    assets = []
    for a in raw_assets:
        t = str(a).upper().strip().removeprefix("$")
        if _TICKER.match(t) and t not in assets:
            assets.append(t)
    return NewsAnalysis(
        sentiment=sentiment, magnitude=int(round(_clamp(data.get("magnitude"), 1, 5, 1))),
        confidence=round(_clamp(data.get("confidence"), 0, 1, 0.5), 3), event_type=event_type,
        assets=assets[:8], summary_pt=_text(data.get("summary_pt"), 300),
    )


# -- market brief -------------------------------------------------------------
BRIEF_SYSTEM = (
    "You are a disciplined crypto derivatives analyst. You receive a factual snapshot (indicators, regime, "
    "funding/open interest, news verdicts, the trading system's own current signals). Summarize what the "
    "data says, flag conflicts between signals, and state a bias ONLY if the data clearly supports it. "
    "Never invent numbers that are not in the snapshot. Answer with ONE JSON object only."
)


def build_brief_prompt(symbol: str, snapshot: dict) -> str:
    lines = "\n".join(f"- {k}: {v}" for k, v in snapshot.items() if v is not None)
    return (
        f"Asset: {symbol}\nSnapshot:\n{lines}\n\n"
        "Return JSON with keys:\n"
        '"bias": "LONG"|"SHORT"|"NEUTRAL",\n'
        '"conviction": integer 0-100,\n'
        '"risk_level": "LOW"|"NORMAL"|"HIGH",\n'
        '"key_points": list of up to 4 short strings in Brazilian Portuguese,\n'
        '"summary_pt": 2-3 sentences in Brazilian Portuguese'
    )


@dataclass(slots=True)
class MarketBrief:
    bias: str
    conviction: int
    risk_level: str
    key_points: list[str] = field(default_factory=list)
    summary_pt: str = ""


def parse_market_brief(data: dict | None) -> MarketBrief | None:
    if not isinstance(data, dict):
        return None
    bias = str(data.get("bias", "")).upper().strip()
    if bias not in BIASES:
        return None
    risk = str(data.get("risk_level", "NORMAL")).upper().strip()
    points = data.get("key_points") or []
    if not isinstance(points, list):
        points = []
    return MarketBrief(
        bias=bias, conviction=int(round(_clamp(data.get("conviction"), 0, 100, 0))),
        risk_level=risk if risk in RISK_LEVELS else "NORMAL",
        key_points=[_text(p, 160) for p in points[:4] if str(p).strip()],
        summary_pt=_text(data.get("summary_pt"), 600),
    )


# -- trade review -------------------------------------------------------------
REVIEW_SYSTEM = (
    "You are a trading coach reviewing ONE closed trade of an automated crypto system. Use only the facts "
    "given (entry reasons, stop/target, max favorable/adverse excursion in R, exit). Classify it honestly: "
    "a loss that followed the plan with a sound setup is CORRECT_LOSS or UNLUCKY, not a mistake. "
    "Answer with ONE JSON object only."
)


def build_review_prompt(trade: dict) -> str:
    lines = "\n".join(f"- {k}: {v}" for k, v in trade.items() if v is not None)
    return (
        f"Trade:\n{lines}\n\n"
        "Return JSON with keys:\n"
        f'"verdict": one of {sorted(VERDICTS)},\n'
        '"lesson_pt": one or two sentences in Brazilian Portuguese with the concrete takeaway'
    )


@dataclass(slots=True)
class TradeReview:
    verdict: str
    lesson_pt: str


def parse_trade_review(data: dict | None) -> TradeReview | None:
    if not isinstance(data, dict):
        return None
    verdict = str(data.get("verdict", "")).upper().strip()
    if verdict not in VERDICTS:
        return None
    lesson = _text(data.get("lesson_pt"), 400)
    if not lesson:
        return None
    return TradeReview(verdict=verdict, lesson_pt=lesson)
