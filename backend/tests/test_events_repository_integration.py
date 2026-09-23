"""Integration tests for EventsRepository against a real TimescaleDB.
Skipped automatically if the database is unreachable (see conftest.py).
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from aegis.db.events_repository import EventsRepository
from aegis.events.models import CoinMarketCalEvent

_T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _event_id() -> str:
    return f"test_{uuid.uuid4().hex[:10]}"


def _event(event_id: str, title: str = "Test event", date=_T0, updated_at=_T0) -> CoinMarketCalEvent:
    return CoinMarketCalEvent(
        event_id=event_id, slug=f"{event_id}-slug", title=title, description=None,
        date=date, date_end=None, date_type="date", is_estimated=False,
        displayed_date="1 Jan", coins=["bitcoin"], impact=None, impact_summary=None,
        source_url=None, snapshot_url=None, last_verified_at=None,
        created_at=_T0, updated_at=updated_at,
    )


@pytest.mark.asyncio
async def test_upsert_and_fetch_upcoming_round_trips(pool):
    repo = EventsRepository(pool)
    event_id = _event_id()
    await repo.upsert_events([_event(event_id)])

    upcoming = await repo.fetch_upcoming(since=_T0 - timedelta(days=1))
    matching = [e for e in upcoming if e["event_id"] == event_id]
    assert len(matching) == 1
    assert matching[0]["title"] == "Test event"
    assert matching[0]["coins"] == ["bitcoin"]


@pytest.mark.asyncio
async def test_upsert_same_event_id_replaces_in_place_not_duplicates(pool):
    repo = EventsRepository(pool)
    event_id = _event_id()
    await repo.upsert_events([_event(event_id, title="Original title")])
    await repo.upsert_events([_event(event_id, title="Updated title", updated_at=_T0 + timedelta(hours=1))])

    upcoming = await repo.fetch_upcoming(since=_T0 - timedelta(days=1))
    matching = [e for e in upcoming if e["event_id"] == event_id]
    assert len(matching) == 1  # not 2 - same event_id must overwrite, not accumulate
    assert matching[0]["title"] == "Updated title"


@pytest.mark.asyncio
async def test_fetch_upcoming_excludes_events_before_the_cutoff(pool):
    repo = EventsRepository(pool)
    past_id, future_id = _event_id(), _event_id()
    await repo.upsert_events([
        _event(past_id, title="Past event", date=_T0),
        _event(future_id, title="Future event", date=_T0 + timedelta(days=365)),
    ])

    upcoming = await repo.fetch_upcoming(since=_T0 + timedelta(days=1), limit=100)
    ids = {e["event_id"] for e in upcoming}
    assert future_id in ids
    assert past_id not in ids


@pytest.mark.asyncio
async def test_upsert_events_with_empty_list_is_a_no_op(pool):
    repo = EventsRepository(pool)
    await repo.upsert_events([])  # must not raise
