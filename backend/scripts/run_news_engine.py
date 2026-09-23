#!/usr/bin/env python
"""Runs the Phase 6/6b News Engine forever: polls every feed in
news_sources.yaml, classifies each entry (assets mentioned, sentiment,
magnitude, source quality score), persists it, then updates the
NEWS_CONFLICT / WAIT_FOR_CONFIRMATION / CONFIRMED verdict for every
tracked asset. No API key needed - every seeded source is a public RSS feed.

Usage (from backend/, venv active):
    python scripts/run_news_engine.py

Ctrl+C to stop.
"""
from __future__ import annotations

import asyncio
import dataclasses
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aegis.config import get_settings  # noqa: E402
from aegis.db.engine import close_pool, create_pool  # noqa: E402
from aegis.db.news_repository import NewsRepository  # noqa: E402
from aegis.logging_utils import configure_logging, get_logger, log_event  # noqa: E402
from aegis.news.service import NewsEngine  # noqa: E402
from aegis.news.source_config import load_news_sources  # noqa: E402
from aegis.providers.rss.client import RssFeedProvider  # noqa: E402

_LOG = get_logger("scripts.run_news_engine")


async def _main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)

    source_configs = load_news_sources(settings.news_sources_config_path)
    log_event(_LOG, "startup", sources=[c.source_id for c in source_configs],
              poll_interval=settings.news_poll_interval_seconds)

    pool = await create_pool(settings)
    provider = RssFeedProvider()
    engine = NewsEngine(
        provider=provider, repository=NewsRepository(pool), source_configs=source_configs,
        conflict_window_hours=settings.news_conflict_window_hours,
    )
    try:
        while True:
            results = await engine.run_once()
            for source_id, items in results.items():
                for item in items:
                    log_event(
                        _LOG, "news_item", source_id=source_id,
                        sentiment=item.sentiment, magnitude=item.magnitude, assets=item.assets,
                        payload=dataclasses.asdict(item),
                    )
                log_event(_LOG, "news_source_polled", source_id=source_id, item_count=len(items))

            conflict_results = await engine.update_conflict_statuses()
            for asset, status in conflict_results.items():
                if status.status != "NO_NEWS":
                    log_event(_LOG, "news_asset_status", asset=asset, status=status.status,
                               distinct_sources=status.distinct_sources, dominant_sentiment=status.dominant_sentiment)

            await asyncio.sleep(settings.news_poll_interval_seconds)
    finally:
        await provider.aclose()
        await close_pool(pool)


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
