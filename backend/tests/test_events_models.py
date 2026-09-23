from datetime import UTC, datetime

from aegis.events.models import CoinMarketCalEvent

# Both captured live against api.coinmarketcal.com 2026-09-23 - one with a
# real dateEnd + isEstimated=False, one estimated with dateEnd="".
EVENT_WITH_RANGE = {
    "id": "98704", "slug": "harmonia-rfp-opens-40878241-1", "title": "Harmonia RFP opens",
    "description": None, "date": "2026-09-16T00:00:00Z", "dateEnd": "2026-10-24T00:00:00Z",
    "dateType": "date", "isEstimated": False, "displayedDate": "16 Sep → 24 Oct",
    "coins": [{"slug": "solana", "symbol": "sol", "name": "Solana"}],
    "impact": None, "impactSummary": None, "sourceUrl": None, "snapshotUrl": None,
    "lastVerifiedAt": None, "createdAt": "2026-09-17T18:42:51Z", "updatedAt": "2026-09-17T18:42:52Z",
}

EVENT_ESTIMATED_NO_END = {
    "id": "98629", "slug": "glamsterdam-testnets-59318741-1", "title": "Glamsterdam testnets",
    "description": None, "date": "2026-09-30T00:00:00Z", "dateEnd": "", "dateType": "month",
    "isEstimated": True, "displayedDate": "Sep 2026",
    "coins": [{"slug": "ethereum", "symbol": "eth", "name": "Ethereum"}],
    "impact": None, "impactSummary": None, "sourceUrl": None, "snapshotUrl": None,
    "lastVerifiedAt": None, "createdAt": "2026-09-16T13:00:53Z", "updatedAt": "2026-09-20T08:45:53Z",
}


def test_parses_event_with_a_real_date_range():
    event = CoinMarketCalEvent.from_api_payload(EVENT_WITH_RANGE)
    assert event.event_id == "98704"  # kept as a string, not coerced to int
    assert event.title == "Harmonia RFP opens"
    assert event.date == datetime(2026, 9, 16, tzinfo=UTC)
    assert event.date_end == datetime(2026, 10, 24, tzinfo=UTC)
    assert event.is_estimated is False
    assert event.coins == ["solana"]


def test_parses_estimated_event_with_no_end_date_as_none_not_empty_string():
    event = CoinMarketCalEvent.from_api_payload(EVENT_ESTIMATED_NO_END)
    assert event.is_estimated is True
    assert event.date_end is None  # "" from the API must become None, not ""
    assert event.displayed_date == "Sep 2026"
    assert event.coins == ["ethereum"]


def test_pro_plus_only_fields_are_none_on_the_free_tier():
    event = CoinMarketCalEvent.from_api_payload(EVENT_WITH_RANGE)
    assert event.impact is None
    assert event.impact_summary is None
    assert event.source_url is None
    assert event.snapshot_url is None
    assert event.last_verified_at is None
