from datetime import UTC, datetime, timedelta

from aegis.news.conflict import AssetNewsStatus, NewsItemForConflict, analyze_asset_conflict, most_recent_status_as_of

_NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)


def _item(source_id, sentiment, quality="SPECIALIZED_CRYPTO_MEDIA", minutes_ago=0):
    return NewsItemForConflict(source_id=source_id, source_quality=quality, sentiment=sentiment,
                                published_at=_NOW - timedelta(minutes=minutes_ago))


def test_no_news_when_no_items():
    status = analyze_asset_conflict("BTC", [])
    assert status.status == "NO_NEWS"
    assert status.dominant_sentiment is None


def test_wait_for_confirmation_with_a_single_source():
    items = [_item("coindesk", "positive"), _item("coindesk", "positive", minutes_ago=30)]
    status = analyze_asset_conflict("BTC", items)
    assert status.status == "WAIT_FOR_CONFIRMATION"
    assert status.distinct_sources == 1
    assert status.dominant_sentiment == "positive"  # the most recent item from that lone source


def test_confirmed_when_two_sources_agree():
    items = [_item("coindesk", "positive"), _item("cointelegraph", "positive")]
    status = analyze_asset_conflict("BTC", items)
    assert status.status == "CONFIRMED"
    assert status.distinct_sources == 2
    assert status.dominant_sentiment == "positive"


def test_confirmed_when_one_source_is_neutral_and_the_other_has_a_verdict():
    items = [_item("coindesk", "positive"), _item("cointelegraph", "neutral")]
    status = analyze_asset_conflict("BTC", items)
    assert status.status == "CONFIRMED"
    assert status.dominant_sentiment == "positive"


def test_news_conflict_when_two_non_primary_sources_disagree():
    items = [_item("coindesk", "positive"), _item("cointelegraph", "negative")]
    status = analyze_asset_conflict("BTC", items)
    assert status.status == "NEWS_CONFLICT"
    assert status.dominant_sentiment is None


def test_primary_official_source_breaks_a_tie_instead_of_conflicting():
    items = [
        _item("coindesk", "negative", quality="SPECIALIZED_CRYPTO_MEDIA"),
        _item("sec_press_releases", "positive", quality="PRIMARY_OFFICIAL"),
    ]
    status = analyze_asset_conflict("BTC", items)
    assert status.status == "CONFIRMED"
    assert status.dominant_sentiment == "positive"  # the primary source's verdict wins


def test_uses_the_latest_item_per_source_not_every_item():
    # coindesk flip-flopped: negative then positive (more recent) - only the
    # latest should count, so this should read as an agreement, not a conflict
    items = [
        _item("coindesk", "negative", minutes_ago=120),
        _item("coindesk", "positive", minutes_ago=5),
        _item("cointelegraph", "positive", minutes_ago=10),
    ]
    status = analyze_asset_conflict("BTC", items)
    assert status.status == "CONFIRMED"
    assert status.dominant_sentiment == "positive"


def test_items_without_published_at_still_get_processed():
    items = [
        NewsItemForConflict(source_id="a", source_quality="SPECIALIZED_CRYPTO_MEDIA",
                             sentiment="positive", published_at=None),
        NewsItemForConflict(source_id="b", source_quality="SPECIALIZED_CRYPTO_MEDIA",
                             sentiment="positive", published_at=None),
    ]
    status = analyze_asset_conflict("BTC", items)
    assert status.status == "CONFIRMED"
    assert status.item_count == 2


# -- most_recent_status_as_of ------------------------------------------------

def _status(sentiment: str) -> AssetNewsStatus:
    return AssetNewsStatus(asset="BTC", status="CONFIRMED", distinct_sources=2, item_count=2,
                            dominant_sentiment=sentiment)


def test_most_recent_status_as_of_empty_history_returns_none():
    assert most_recent_status_as_of([], _NOW) is None


def test_most_recent_status_as_of_ignores_everything_after_the_cutoff():
    # The critical no-lookahead case: a status computed AFTER as_of must
    # never be returned, even if it's the only entry in history.
    future_status = (_NOW + timedelta(hours=1), _status("positive"))
    assert most_recent_status_as_of([future_status], _NOW) is None


def test_most_recent_status_as_of_picks_the_latest_entry_at_or_before_cutoff():
    history = [
        (_NOW - timedelta(hours=3), _status("neutral")),
        (_NOW - timedelta(hours=1), _status("positive")),  # should win - latest <= cutoff
        (_NOW + timedelta(hours=1), _status("negative")),  # after cutoff - must be ignored
    ]
    result = most_recent_status_as_of(history, _NOW)
    assert result.dominant_sentiment == "positive"


def test_most_recent_status_as_of_is_inclusive_of_the_exact_cutoff_instant():
    exact = (_NOW, _status("negative"))
    result = most_recent_status_as_of([exact], _NOW)
    assert result.dominant_sentiment == "negative"


def test_most_recent_status_as_of_does_not_require_sorted_input():
    history = [
        (_NOW - timedelta(hours=1), _status("positive")),
        (_NOW - timedelta(hours=5), _status("neutral")),
        (_NOW - timedelta(hours=2), _status("negative")),
    ]
    result = most_recent_status_as_of(history, _NOW)
    assert result.dominant_sentiment == "positive"
