"""Listing Radar: new-listing announcements from Binance, Upbit and OKX.

Alert-only by design. An event study over 437 real announcements
(2025-2026, entry at the 2nd minute after the announcement on the token's
USDT-M perpetual) found the move is almost entirely priced in before a
polling bot can act: +20% mean / +9-11% median from 5 minutes before the
announcement to our earliest realistic entry, then a ~0 median return over
the next hour and -3% over 4h for Upbit KRW listings. Latency-competitive
bots take that move in milliseconds; auto-buying it here would mean
systematically buying the top. What the radar is good for: knowing why a
token is moving, and not opening a trend entry into a listing spike.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

_UA = {"User-Agent": "Mozilla/5.0 (AegisQuant listing radar)"}
_PAREN_TICKER = re.compile(r"\(([A-Z0-9]{2,12})\)")
_PERP = re.compile(r"\b([A-Z0-9]{2,15})USDT\b")
_NON_CRYPTO_HINTS = ("bStock", "Stock", "TradFi", "Pre-IPO", "Tokenized Securities", "Dividend")


@dataclass(slots=True)
class ListingEvent:
    source: str   # BINANCE_SPOT | BINANCE_FUTURES | BINANCE_ALPHA | UPBIT_KRW | UPBIT | OKX_SPOT
    ticker: str
    title: str
    announced_at: datetime


def parse_binance_articles(articles: list[dict]) -> list[ListingEvent]:
    events: list[ListingEvent] = []
    for a in articles:
        title = a.get("title", "")
        if any(h in title for h in _NON_CRYPTO_HINTS):
            continue
        when = datetime.fromtimestamp(int(a["releaseDate"]) / 1000, UTC)
        if "Binance Will List" in title:
            source, tickers = "BINANCE_SPOT", _PAREN_TICKER.findall(title)
        elif "Binance Alpha" in title:
            source, tickers = "BINANCE_ALPHA", _PAREN_TICKER.findall(title)
        elif "Binance Futures Will Launch" in title and "Perpetual" in title:
            source, tickers = "BINANCE_FUTURES", [t.removeprefix("1000") for t in _PERP.findall(title)]
        else:
            continue
        events += [ListingEvent(source, t, title, when) for t in dict.fromkeys(tickers)]
    return events


def parse_upbit_notices(notices: list[dict]) -> list[ListingEvent]:
    events: list[ListingEvent] = []
    for n in notices:
        title = n.get("title", "")
        if "신규 거래지원" not in title and "디지털 자산 추가" not in title:
            continue
        when = datetime.fromisoformat(n["listed_at"]).astimezone(UTC)
        source = "UPBIT_KRW" if "KRW" in title else "UPBIT"
        events += [ListingEvent(source, t, title, when) for t in dict.fromkeys(_PAREN_TICKER.findall(title))]
    return events


_OKX_LIST = re.compile(r"OKX (?:to|will) list ([A-Z0-9]{2,12})/USDT", re.IGNORECASE)


def parse_okx_announcements(details: list[dict]) -> list[ListingEvent]:
    events: list[ListingEvent] = []
    for d in details:
        m = _OKX_LIST.search(d.get("title", ""))
        if not m:
            continue
        when = datetime.fromtimestamp(int(d["pTime"]) / 1000, UTC)
        events.append(ListingEvent("OKX_SPOT", m.group(1).upper(), d["title"], when))
    return events


class ListingRadarClient:
    def __init__(self, http: httpx.AsyncClient | None = None) -> None:
        self._http = http or httpx.AsyncClient(timeout=20, headers=_UA)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def fetch_all(self) -> list[ListingEvent]:
        events: list[ListingEvent] = []
        try:
            r = await self._http.get("https://www.binance.com/bapi/composite/v1/public/cms/article/list/query",
                                     params={"type": 1, "catalogId": 48, "pageNo": 1, "pageSize": 20})
            events += parse_binance_articles(r.json()["data"]["catalogs"][0]["articles"])
        except (httpx.HTTPError, ValueError, KeyError, IndexError):
            pass
        try:
            r = await self._http.get("https://api-manager.upbit.com/api/v1/announcements",
                                     params={"os": "web", "page": 1, "per_page": 20, "category": "trade"})
            events += parse_upbit_notices(r.json()["data"]["notices"])
        except (httpx.HTTPError, ValueError, KeyError):
            pass
        try:
            r = await self._http.get("https://www.okx.com/api/v5/support/announcements",
                                     params={"annType": "announcements-new-listings"})
            events += parse_okx_announcements(r.json()["data"][0]["details"])
        except (httpx.HTTPError, ValueError, KeyError, IndexError):
            pass
        return events
