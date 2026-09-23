import pytest

from aegis.events.models import CoinMarketCalEvent
from aegis.events.service import EventRiskEngine


class _FakeProvider:
    def __init__(self, payload):
        self.payload = payload
        self.calls: list[tuple] = []

    async def get_events(self, coins, limit=50):
        self.calls.append((coins, limit))
        return self.payload


class _FakeRepo:
    def __init__(self):
        self.upserted: list[CoinMarketCalEvent] = []

    async def upsert_events(self, events):
        self.upserted.extend(events)


_PAYLOAD = [{
    "id": "98629", "slug": "glamsterdam-testnets-59318741-1", "title": "Glamsterdam testnets",
    "description": None, "date": "2026-09-30T00:00:00Z", "dateEnd": "", "dateType": "month",
    "isEstimated": True, "displayedDate": "Sep 2026",
    "coins": [{"slug": "ethereum", "symbol": "eth", "name": "Ethereum"}],
    "impact": None, "impactSummary": None, "sourceUrl": None, "snapshotUrl": None,
    "lastVerifiedAt": None, "createdAt": "2026-09-16T13:00:53Z", "updatedAt": "2026-09-20T08:45:53Z",
}]


@pytest.mark.asyncio
async def test_run_once_parses_and_persists_events():
    provider = _FakeProvider(_PAYLOAD)
    repo = _FakeRepo()
    engine = EventRiskEngine(provider, repo)

    events = await engine.run_once(["bitcoin", "ethereum"])

    assert provider.calls == [(["bitcoin", "ethereum"], 50)]
    assert len(events) == 1
    assert events[0].title == "Glamsterdam testnets"
    assert repo.upserted == events


@pytest.mark.asyncio
async def test_run_once_with_no_events_persists_nothing():
    provider = _FakeProvider([])
    repo = _FakeRepo()
    engine = EventRiskEngine(provider, repo)

    events = await engine.run_once(["bitcoin"])

    assert events == []
    assert repo.upserted == []
