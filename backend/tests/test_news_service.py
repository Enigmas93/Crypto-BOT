from datetime import UTC, datetime

import pytest

from aegis.news.conflict import NewsItemForConflict
from aegis.news.service import NewsEngine, classify_news_item
from aegis.news.source_config import NewsSourceConfig


def _source(source_id="fed", quality="PRIMARY_OFFICIAL") -> NewsSourceConfig:
    return NewsSourceConfig(source_id=source_id, name="Test Source",
                             feed_url=f"https://example.gov/{source_id}.rss",
                             format="rss", source_quality=quality)


def test_classify_news_item_populates_every_field():
    raw = {
        "title": "Regulator approves Bitcoin ETF listing",
        "link": "https://example.gov/1",
        "guid": "https://example.gov/1",
        "summary": "The approval marks a milestone for Bitcoin adoption",
        "published_at": datetime(2026, 9, 18, 15, 0, tzinfo=UTC),
    }
    item = classify_news_item(_source(), raw)

    assert item.source_id == "fed"
    assert item.guid == "https://example.gov/1"
    assert item.title == "Regulator approves Bitcoin ETF listing"
    assert "BTC" in item.assets
    assert item.sentiment == "positive"
    assert item.magnitude == "HIGH"  # "approval" + "etf" keyword
    assert item.source_quality == "PRIMARY_OFFICIAL"
    assert item.source_quality_score == pytest.approx(1.00)


def test_classify_news_item_returns_none_without_guid_or_title():
    assert classify_news_item(_source(), {"title": "Has a title", "guid": ""}) is None
    assert classify_news_item(_source(), {"title": "", "guid": "x"}) is None
    assert classify_news_item(_source(), {}) is None


class _FakeProvider:
    def __init__(self, entries_by_url: dict[str, list[dict]]):
        self._data = entries_by_url
        self.calls: list[str] = []

    async def poll(self, feed_url):
        self.calls.append(feed_url)
        return self._data.get(feed_url, [])


class _FakeRepo:
    def __init__(self, items_by_asset: dict[str, list[NewsItemForConflict]] | None = None):
        self.registered: list = []
        self.items_inserted: list = []
        self.statuses_written: list = []
        self._items_by_asset = items_by_asset or {}

    async def upsert_source_registry(self, configs):
        self.registered.extend(configs)

    async def insert_news_items(self, items):
        self.items_inserted.extend(items)

    async def fetch_recent_by_asset(self, asset, window_hours):
        return self._items_by_asset.get(asset, [])

    async def upsert_asset_status(self, status, window_hours):
        if status.status == "NO_NEWS":  # mirrors the real NewsRepository's skip
            return
        self.statuses_written.append(status)


@pytest.mark.asyncio
async def test_poll_and_store_one_classifies_and_persists_every_valid_entry():
    source = _source()
    provider = _FakeProvider({
        source.feed_url: [
            {"title": "Fed announces rate decision", "guid": "g1", "link": "https://x/1", "summary": ""},
            {"title": "", "guid": "g2"},  # dropped - no title
        ],
    })
    repo = _FakeRepo()
    engine = NewsEngine(provider, repo, [source])

    items = await engine.poll_and_store_one(source)

    assert len(items) == 1
    assert items[0].guid == "g1"
    assert len(repo.items_inserted) == 1
    assert repo.registered == [source]


@pytest.mark.asyncio
async def test_run_once_survives_one_source_failing():
    class _FlakyProvider(_FakeProvider):
        async def poll(self, feed_url):
            raise RuntimeError("feed unreachable")

    sources = [_source("fed"), _source("sec")]
    engine = NewsEngine(_FlakyProvider({}), _FakeRepo(), sources)

    results = await engine.run_once()  # must not raise

    assert results == {}


@pytest.mark.asyncio
async def test_run_once_polls_every_configured_source():
    fed = _source("fed")
    sec = _source("sec")
    provider = _FakeProvider({
        fed.feed_url: [{"title": "A", "guid": "g1"}],
        sec.feed_url: [{"title": "B", "guid": "g2"}],
    })
    repo = _FakeRepo()
    engine = NewsEngine(provider, repo, [fed, sec])

    results = await engine.run_once()

    assert set(results.keys()) == {"fed", "sec"}
    assert len(repo.items_inserted) == 2


@pytest.mark.asyncio
async def test_update_conflict_statuses_covers_the_configured_asset_universe():
    btc_items = [
        NewsItemForConflict(source_id="coindesk", source_quality="SPECIALIZED_CRYPTO_MEDIA",
                             sentiment="positive", published_at=datetime(2026, 9, 18, tzinfo=UTC)),
    ]
    repo = _FakeRepo(items_by_asset={"BTC": btc_items})
    engine = NewsEngine(_FakeProvider({}), repo, [], asset_universe=["BTC", "ETH"])

    results = await engine.update_conflict_statuses()

    assert results["BTC"].status == "WAIT_FOR_CONFIRMATION"
    assert results["ETH"].status == "NO_NEWS"
    # NO_NEWS is skipped by the repo's upsert, not written - only BTC's real status is
    assert len(repo.statuses_written) == 1
    assert repo.statuses_written[0].asset == "BTC"


@pytest.mark.asyncio
async def test_update_conflict_statuses_survives_one_asset_failing():
    class _FlakyRepo(_FakeRepo):
        async def fetch_recent_by_asset(self, asset, window_hours):
            if asset == "ETH":
                raise RuntimeError("db exploded")
            return []

    engine = NewsEngine(_FakeProvider({}), _FlakyRepo(), [], asset_universe=["BTC", "ETH"])

    results = await engine.update_conflict_statuses()  # must not raise

    assert "ETH" not in results
    assert results["BTC"].status == "NO_NEWS"


def test_default_asset_universe_matches_the_classifiers_known_aliases():
    from aegis.news.classifier import DEFAULT_ASSET_ALIASES

    engine = NewsEngine(_FakeProvider({}), _FakeRepo(), [])
    assert set(engine.asset_universe) == set(DEFAULT_ASSET_ALIASES.keys())
