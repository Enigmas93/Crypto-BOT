from pathlib import Path

from aegis.news.source_config import load_news_sources

REPO_YAML = Path(__file__).resolve().parents[1] / "news_sources.yaml"


def test_load_news_sources_parses_a_minimal_file(tmp_path):
    yaml_path = tmp_path / "sources.yaml"
    yaml_path.write_text(
        """
sources:
  - source_id: test_feed
    name: "Test Feed"
    feed_url: "https://example.gov/feed.rss"
    format: rss
    source_quality: PRIMARY_OFFICIAL
""",
        encoding="utf-8",
    )

    configs = load_news_sources(yaml_path)

    assert len(configs) == 1
    assert configs[0].source_id == "test_feed"
    assert configs[0].source_quality == "PRIMARY_OFFICIAL"


def test_load_news_sources_empty_file_returns_empty_list(tmp_path):
    yaml_path = tmp_path / "empty.yaml"
    yaml_path.write_text("sources: []\n", encoding="utf-8")
    assert load_news_sources(yaml_path) == []


def test_the_real_news_sources_yaml_in_the_repo_loads_and_has_unique_ids():
    configs = load_news_sources(REPO_YAML)
    assert len(configs) >= 9
    ids = [c.source_id for c in configs]
    assert len(ids) == len(set(ids))
    for c in configs:
        assert c.format in {"rss", "atom"}
        assert c.source_quality in {
            "PRIMARY_OFFICIAL", "TIER_1_FINANCIAL_MEDIA", "SPECIALIZED_CRYPTO_MEDIA",
            "SOCIAL_MEDIA", "UNVERIFIED",
        }
        assert c.feed_url.startswith("https://")


def test_the_real_news_sources_yaml_covers_multiple_quality_tiers():
    # Phase 6b's whole point: not just PRIMARY_OFFICIAL anymore
    configs = load_news_sources(REPO_YAML)
    tiers = {c.source_quality for c in configs}
    assert {"PRIMARY_OFFICIAL", "TIER_1_FINANCIAL_MEDIA", "SPECIALIZED_CRYPTO_MEDIA"} <= tiers
