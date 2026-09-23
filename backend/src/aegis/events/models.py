"""CoinMarketCal event types - see providers/coinmarketcal/client.py for
API verification notes.

`date` is NOT always the real event date - when `is_estimated` is true, the
API's own docs are explicit that `date` is a deadline/window-end, and
`displayed_date` (a pre-formatted string like "Q3 2026" or "16 Sep → 24
Oct") is what must be shown to a human instead. Never render `date`
directly without checking `is_estimated` first - that's the exact mistake
the API's docs warn against.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


def _parse_dt(value: str | None) -> datetime | None:
    if not value:  # None or "" - CoinMarketCal uses "" for "no end date"
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@dataclass(slots=True)
class CoinMarketCalEvent:
    event_id: str  # CoinMarketCal's ids are strings, not ints
    slug: str
    title: str
    description: str | None
    date: datetime
    date_end: datetime | None
    date_type: str  # "date" | "month" | "quarter" | "year", observed live
    is_estimated: bool
    displayed_date: str
    coins: list[str]  # canonical CoinMarketCal slugs, e.g. "bitcoin"
    impact: float | None  # Pro+ only - always None on the free tier
    impact_summary: str | None  # Pro+ only
    source_url: str | None  # Pro+ only
    snapshot_url: str | None  # Pro+ only
    last_verified_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_api_payload(cls, payload: dict[str, Any]) -> "CoinMarketCalEvent":
        return cls(
            event_id=str(payload["id"]),
            slug=payload["slug"],
            title=payload["title"],
            description=payload.get("description"),
            date=_parse_dt(payload["date"]),
            date_end=_parse_dt(payload.get("dateEnd")),
            date_type=payload["dateType"],
            is_estimated=bool(payload["isEstimated"]),
            displayed_date=payload["displayedDate"],
            coins=[c["slug"] for c in payload.get("coins", [])],
            impact=payload.get("impact"),
            impact_summary=payload.get("impactSummary"),
            source_url=payload.get("sourceUrl"),
            snapshot_url=payload.get("snapshotUrl"),
            last_verified_at=_parse_dt(payload.get("lastVerifiedAt")),
            created_at=_parse_dt(payload["createdAt"]),
            updated_at=_parse_dt(payload["updatedAt"]),
        )
