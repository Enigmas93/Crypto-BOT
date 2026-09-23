"""Loads `news_sources.yaml` (spec section 38) - the single source of
truth for which feeds exist, never a feed_url assumed inline in code."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(slots=True)
class NewsSourceConfig:
    source_id: str
    name: str
    feed_url: str
    format: str  # rss | atom
    source_quality: str  # PRIMARY_OFFICIAL | TIER_1_FINANCIAL_MEDIA | SPECIALIZED_CRYPTO_MEDIA | SOCIAL_MEDIA | UNVERIFIED


def load_news_sources(path: str | Path) -> list[NewsSourceConfig]:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    entries = raw.get("sources", [])
    return [NewsSourceConfig(**entry) for entry in entries]
