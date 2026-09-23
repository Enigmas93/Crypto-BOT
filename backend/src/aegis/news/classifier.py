"""Rule-based news classification - spec sections 39/40.

Deliberately NOT machine-learned or LLM-based: every function here is a
transparent, deterministic keyword/lexicon heuristic, documented as
exactly that. Spec section 83 explicitly reserves "classificar
notícias... classificar sentimento" as a legitimate role for a future AI
component - this is the honest v1 that a smarter classifier replaces
later, not a claim of NLP-grade accuracy today. `event_type` and
`time_sensitivity` (also listed in spec section 39) are deliberately not
implemented here: a keyword heuristic for either would mostly duplicate
`magnitude` with extra steps, and doing them properly needs real
classification, not more keyword lists.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Spec section 40 - the five tiers and their exact scores.
SOURCE_QUALITY_SCORES: dict[str, float] = {
    "PRIMARY_OFFICIAL": 1.00,
    "TIER_1_FINANCIAL_MEDIA": 0.90,
    "SPECIALIZED_CRYPTO_MEDIA": 0.75,
    "SOCIAL_MEDIA": 0.30,
    "UNVERIFIED": 0.00,
}


def source_quality_score(source_quality: str) -> float:
    return SOURCE_QUALITY_SCORES.get(source_quality, SOURCE_QUALITY_SCORES["UNVERIFIED"])


# Deliberately small and editable - covers the majors this system tracks
# by default (spec section 11's Tier A/B). Extend as the asset universe grows.
DEFAULT_ASSET_ALIASES: dict[str, list[str]] = {
    "BTC": ["BTC", "Bitcoin"],
    "ETH": ["ETH", "Ethereum", "Ether"],
    "SOL": ["SOL", "Solana"],
    "XRP": ["XRP", "Ripple"],
    "BNB": ["BNB", "Binance Coin"],
    "DOGE": ["DOGE", "Dogecoin"],
    "ADA": ["ADA", "Cardano"],
}


def extract_assets(text: str, aliases: dict[str, list[str]] = DEFAULT_ASSET_ALIASES) -> list[str]:
    """Which tracked assets a news item mentions, by whole-word keyword
    match (case-insensitive). "ETH" won't match inside "method"."""
    found = []
    for symbol, keywords in aliases.items():
        pattern = "|".join(re.escape(k) for k in keywords)
        if re.search(rf"\b({pattern})\b", text, flags=re.IGNORECASE):
            found.append(symbol)
    return found


_POSITIVE_KEYWORDS = {
    "approval", "approved", "approves", "partnership", "launch", "launches", "launched",
    "listing", "lists", "upgrade", "upgraded", "adoption", "integration", "record high",
    "bullish", "surge", "surges", "rally", "rallies", "milestone", "expansion",
}
_NEGATIVE_KEYWORDS = {
    "hack", "hacked", "exploit", "exploited", "lawsuit", "sued", "sues", "ban", "banned",
    "fraud", "scam", "bankruptcy", "delisting", "delisted", "vulnerability", "breach",
    "investigation", "charges", "charged", "halt", "halted", "crash", "crashes", "outage",
    "enforcement",
}
_HIGH_IMPACT_KEYWORDS = {
    # Deliberately excludes agency self-references ("sec", "cftc", "federal
    # reserve", "fomc") - a live smoke test against real SEC/CFTC/Fed feeds
    # showed those match nearly every headline from that source trivially
    # (a press release from the SEC almost always says "SEC" in its own
    # title), which made magnitude=HIGH nearly universal and useless for
    # triage. What's left is content-specific, not source-specific.
    "lawsuit", "hack", "approval", "etf", "bankruptcy", "ban", "enforcement",
    "indictment", "subpoena", "fraud", "charges",
}


@dataclass(slots=True)
class SentimentResult:
    sentiment: str  # positive | negative | neutral
    score: float  # -1.0 .. 1.0
    confidence: float  # 0.0 .. 1.0 - how many lexicon hits backed this call, not statistical certainty


def classify_sentiment(text: str) -> SentimentResult:
    lowered = text.lower()
    positive_hits = sum(1 for kw in _POSITIVE_KEYWORDS if kw in lowered)
    negative_hits = sum(1 for kw in _NEGATIVE_KEYWORDS if kw in lowered)
    total_hits = positive_hits + negative_hits

    if total_hits == 0:
        return SentimentResult(sentiment="neutral", score=0.0, confidence=0.0)

    score = (positive_hits - negative_hits) / total_hits
    if score > 0:
        sentiment = "positive"
    elif score < 0:
        sentiment = "negative"
    else:
        sentiment = "neutral"
    confidence = min(1.0, total_hits / 3)  # 3+ lexicon hits -> full confidence in *this heuristic*, not ground truth
    return SentimentResult(sentiment=sentiment, score=round(score, 3), confidence=round(confidence, 3))


def classify_magnitude(text: str) -> str:
    """HIGH/MEDIUM/LOW - a coarse triage signal, not a claim of measured
    market impact."""
    lowered = text.lower()
    if any(kw in lowered for kw in _HIGH_IMPACT_KEYWORDS):
        return "HIGH"
    if any(kw in lowered for kw in _POSITIVE_KEYWORDS | _NEGATIVE_KEYWORDS):
        return "MEDIUM"
    return "LOW"
