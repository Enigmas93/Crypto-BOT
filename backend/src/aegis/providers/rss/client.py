"""Generic RSS 2.0 / Atom feed client - spec section 38.

No scraping: only parses what a feed's publisher chose to put in its own
XML. One provider instance serves every configured feed URL (RSS or
Atom - auto-detected from the root element), the same shape as
`MacroDataProvider.get_series(series_id)`.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Any
from xml.etree import ElementTree

import httpx

from aegis.logging_utils import get_logger, log_event
from aegis.providers.base import NewsProvider
from aegis.utils.backoff import BackoffPolicy

_LOG = get_logger("rss.client")

_ATOM_NS = "{http://www.w3.org/2005/Atom}"
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
MAX_RETRIES = 3


class RssFeedError(RuntimeError):
    pass


def _parse_date(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return parsedate_to_datetime(raw)  # RSS: RFC 822 ("Tue, 10 Jun 2025 15:00:00 GMT")
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))  # Atom: ISO 8601
    except ValueError:
        return None


def _parse_rss(root: ElementTree.Element) -> list[dict[str, Any]]:
    entries = []
    for item in root.iterfind("./channel/item"):
        entries.append({
            "title": (item.findtext("title") or "").strip(),
            "link": (item.findtext("link") or "").strip(),
            "guid": (item.findtext("guid") or item.findtext("link") or "").strip(),
            "summary": (item.findtext("description") or "").strip(),
            "published_at": _parse_date(item.findtext("pubDate")),
        })
    return entries


def _atom_link(entry: ElementTree.Element) -> str:
    link_el = entry.find(f"{_ATOM_NS}link")
    if link_el is not None:
        return link_el.get("href", "").strip()
    return ""


def _parse_atom(root: ElementTree.Element) -> list[dict[str, Any]]:
    entries = []
    for entry in root.iterfind(f"{_ATOM_NS}entry"):
        summary = entry.findtext(f"{_ATOM_NS}summary") or entry.findtext(f"{_ATOM_NS}content") or ""
        published = entry.findtext(f"{_ATOM_NS}published") or entry.findtext(f"{_ATOM_NS}updated")
        entries.append({
            "title": (entry.findtext(f"{_ATOM_NS}title") or "").strip(),
            "link": _atom_link(entry),
            "guid": (entry.findtext(f"{_ATOM_NS}id") or "").strip(),
            "summary": summary.strip(),
            "published_at": _parse_date(published),
        })
    return entries


class RssFeedProvider(NewsProvider):
    def __init__(self, timeout: float = 10.0) -> None:
        self._client = httpx.AsyncClient(timeout=timeout, follow_redirects=True)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "RssFeedProvider":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def poll(self, feed_url: str) -> list[dict[str, Any]]:
        policy = BackoffPolicy(base_seconds=0.5, max_seconds=6.0)
        last_error: Exception | None = None
        for _ in range(MAX_RETRIES):
            try:
                response = await self._client.get(feed_url, headers={"User-Agent": "AegisQuant/1.0 (+news-engine)"})
            except httpx.TransportError as exc:
                last_error = exc
                log_event(_LOG, "rss_transport_error", level=30, feed_url=feed_url, error=str(exc))
            else:
                if response.status_code == 200:
                    return self._parse(feed_url, response.content)
                if response.status_code not in _RETRYABLE_STATUS:
                    raise RssFeedError(f"{feed_url} -> HTTP {response.status_code}")
                last_error = RssFeedError(f"{feed_url} -> HTTP {response.status_code}")
                log_event(_LOG, "rss_retryable_error", level=30, feed_url=feed_url, status=response.status_code)
            await asyncio.sleep(policy.next_delay())
        raise RssFeedError(f"{feed_url} failed after {MAX_RETRIES} attempts") from last_error

    @staticmethod
    def _parse(feed_url: str, content: bytes) -> list[dict[str, Any]]:
        try:
            root = ElementTree.fromstring(content)
        except ElementTree.ParseError as exc:
            raise RssFeedError(f"{feed_url} -> malformed XML: {exc}") from exc

        if root.tag == "rss":
            return _parse_rss(root)
        if root.tag == f"{_ATOM_NS}feed":
            return _parse_atom(root)
        raise RssFeedError(f"{feed_url} -> unrecognized feed root element <{root.tag}>")
