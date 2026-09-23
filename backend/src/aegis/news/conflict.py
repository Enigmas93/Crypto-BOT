"""NEWS_CONFLICT / WAIT_FOR_CONFIRMATION - spec section 41.

Pure function over already-fetched items, same split as every other
engine's `compute_snapshot`: no I/O here, `NewsEngine` does the fetching.

Spec section 41, verbatim logic:
  - "Se duas fontes entrarem em conflito: não operar imediatamente" ->
    NEWS_CONFLICT when at least two distinct sources disagree in sentiment
    and none of them is a primary/official source to break the tie.
  - "Se uma notícia tiver apenas uma fonte: WAIT_FOR_CONFIRMATION" ->
    exactly that, regardless of what that lone source says.
  - "Se for fonte primária: pode receber maior peso" -> when a
    PRIMARY_OFFICIAL source is among the disagreeing sources, its
    sentiment wins instead of raising a conflict.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

_EPOCH_FALLBACK = datetime.min.replace(tzinfo=UTC)


@dataclass(slots=True)
class NewsItemForConflict:
    source_id: str
    source_quality: str
    sentiment: str  # positive | negative | neutral
    published_at: datetime | None


@dataclass(slots=True)
class AssetNewsStatus:
    asset: str
    status: str  # NO_NEWS | WAIT_FOR_CONFIRMATION | CONFIRMED | NEWS_CONFLICT
    distinct_sources: int
    item_count: int
    dominant_sentiment: str | None


def _effective_time(item: NewsItemForConflict) -> datetime:
    return item.published_at or _EPOCH_FALLBACK


def analyze_asset_conflict(asset: str, items: list[NewsItemForConflict]) -> AssetNewsStatus:
    if not items:
        return AssetNewsStatus(asset=asset, status="NO_NEWS", distinct_sources=0, item_count=0,
                                dominant_sentiment=None)

    latest_per_source: dict[str, NewsItemForConflict] = {}
    for item in items:
        current = latest_per_source.get(item.source_id)
        if current is None or _effective_time(item) > _effective_time(current):
            latest_per_source[item.source_id] = item

    distinct_sources = len(latest_per_source)
    item_count = len(items)

    if distinct_sources == 1:
        only = next(iter(latest_per_source.values()))
        return AssetNewsStatus(asset=asset, status="WAIT_FOR_CONFIRMATION", distinct_sources=1,
                                item_count=item_count, dominant_sentiment=only.sentiment)

    sentiments = {source_id: it.sentiment for source_id, it in latest_per_source.items()}
    non_neutral_sentiments = {s for s in sentiments.values() if s != "neutral"}

    if len(non_neutral_sentiments) <= 1:
        dominant = next(iter(non_neutral_sentiments)) if non_neutral_sentiments else "neutral"
        return AssetNewsStatus(asset=asset, status="CONFIRMED", distinct_sources=distinct_sources,
                                item_count=item_count, dominant_sentiment=dominant)

    primary_items = [it for it in latest_per_source.values() if it.source_quality == "PRIMARY_OFFICIAL"]
    if primary_items:
        # multiple primary sources could in principle still disagree with
        # each other; take the most recent primary verdict as the tie-breaker
        newest_primary = max(primary_items, key=_effective_time)
        return AssetNewsStatus(asset=asset, status="CONFIRMED", distinct_sources=distinct_sources,
                                item_count=item_count, dominant_sentiment=newest_primary.sentiment)

    return AssetNewsStatus(asset=asset, status="NEWS_CONFLICT", distinct_sources=distinct_sources,
                            item_count=item_count, dominant_sentiment=None)


def most_recent_status_as_of(
    history: list[tuple[datetime, AssetNewsStatus]], as_of: datetime,
) -> AssetNewsStatus | None:
    """The point-in-time read a historical replay must use instead of
    "whatever the status is right now" - reading the live/current status
    while evaluating a bar from the past would leak future information
    into that decision (the exact lookahead bug this codebase is built to
    avoid everywhere else - see BacktestEngine's module docstring).
    `history` need not be pre-sorted or pre-filtered by the caller."""
    candidates = [(t, s) for t, s in history if t <= as_of]
    if not candidates:
        return None
    return max(candidates, key=lambda pair: pair[0])[1]
