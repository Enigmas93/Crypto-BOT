"""NewsEngine - spec sections 38 and 41.

Each poll of a feed classifies every entry independently (no windowing/
aggregation needed for that, unlike Derivatives/Liquidation/Macro's
snapshot pattern) and persists it. After every full poll cycle,
`update_conflict_statuses` runs `aegis.news.conflict.analyze_asset_conflict`
(spec section 41 - NEWS_CONFLICT / WAIT_FOR_CONFIRMATION) over the
configured asset universe and persists one verdict per asset.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime

from aegis.logging_utils import get_logger, log_event
from aegis.news.classifier import (
    DEFAULT_ASSET_ALIASES,
    classify_magnitude,
    classify_sentiment,
    extract_assets,
    source_quality_score,
)
from aegis.news.conflict import AssetNewsStatus, analyze_asset_conflict
from aegis.news.source_config import NewsSourceConfig

_LOG = get_logger("news.service")


@dataclass(slots=True)
class ClassifiedNewsItem:
    source_id: str
    guid: str
    title: str
    link: str
    summary: str
    published_at: datetime | None
    assets: list[str]
    sentiment: str
    sentiment_score: float
    confidence: float
    magnitude: str
    source_quality: str
    source_quality_score: float


def classify_news_item(source_config: NewsSourceConfig, raw_entry: dict) -> ClassifiedNewsItem | None:
    guid = (raw_entry.get("guid") or "").strip()
    title = (raw_entry.get("title") or "").strip()
    if not guid or not title:
        return None  # nothing worth persisting without at least an id and a headline

    text = f"{title} {raw_entry.get('summary', '')}"
    sentiment = classify_sentiment(text)

    return ClassifiedNewsItem(
        source_id=source_config.source_id,
        guid=guid,
        title=title,
        link=(raw_entry.get("link") or "").strip(),
        summary=(raw_entry.get("summary") or "").strip(),
        published_at=raw_entry.get("published_at"),
        assets=extract_assets(text),
        sentiment=sentiment.sentiment,
        sentiment_score=sentiment.score,
        confidence=sentiment.confidence,
        magnitude=classify_magnitude(text),
        source_quality=source_config.source_quality,
        source_quality_score=source_quality_score(source_config.source_quality),
    )


class NewsEngine:
    def __init__(
        self, provider, repository, source_configs: list[NewsSourceConfig],
        conflict_window_hours: int = 24, asset_universe: list[str] | None = None,
    ) -> None:
        self.provider = provider
        self.repo = repository
        self.source_configs = source_configs
        self.conflict_window_hours = conflict_window_hours
        # Same vocabulary extract_assets already recognizes - no point
        # checking conflict status for an asset the classifier can never tag.
        self.asset_universe = asset_universe or list(DEFAULT_ASSET_ALIASES.keys())

    async def poll_and_store_one(self, source_config: NewsSourceConfig) -> list[ClassifiedNewsItem]:
        await self.repo.upsert_source_registry([source_config])
        raw_entries = await self.provider.poll(source_config.feed_url)
        items = [
            item for item in (classify_news_item(source_config, e) for e in raw_entries) if item is not None
        ]
        await self.repo.insert_news_items(items)
        return items

    async def run_once(self) -> dict[str, list[ClassifiedNewsItem]]:
        results: dict[str, list[ClassifiedNewsItem]] = {}
        for config in self.source_configs:
            try:
                results[config.source_id] = await self.poll_and_store_one(config)
            except Exception as exc:  # noqa: BLE001 - one bad feed must not skip the rest
                log_event(_LOG, "news_poll_failed", level=40, source_id=config.source_id, error=str(exc))
        return results

    async def update_conflict_statuses(self) -> dict[str, AssetNewsStatus]:
        results: dict[str, AssetNewsStatus] = {}
        for asset in self.asset_universe:
            try:
                items = await self.repo.fetch_recent_by_asset(asset, self.conflict_window_hours)
                status = analyze_asset_conflict(asset, items)
                await self.repo.upsert_asset_status(status, self.conflict_window_hours)
                results[asset] = status
            except Exception as exc:  # noqa: BLE001 - one bad asset must not skip the rest
                log_event(_LOG, "news_conflict_analysis_failed", level=40, asset=asset, error=str(exc))
        return results
