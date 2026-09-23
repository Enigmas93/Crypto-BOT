from datetime import timezone

import httpx
import pytest

from aegis.providers.rss.client import RssFeedError, RssFeedProvider

RSS_XML = b"""<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0">
  <channel>
    <title>Test Feed</title>
    <item>
      <title>Fed announces rate decision</title>
      <link><![CDATA[https://example.gov/press/1]]></link>
      <guid><![CDATA[https://example.gov/press/1]]></guid>
      <description><![CDATA[The Federal Reserve announced today...]]></description>
      <pubDate>Fri, 18 Sep 2026 15:00:00 GMT</pubDate>
    </item>
    <item>
      <title>Second release</title>
      <link>https://example.gov/press/2</link>
      <guid>https://example.gov/press/2</guid>
      <description>Another item</description>
      <pubDate>Thu, 17 Sep 2026 12:30:00 GMT</pubDate>
    </item>
  </channel>
</rss>
"""

ATOM_XML = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Test Atom Feed</title>
  <entry>
    <title>SEC charges filed</title>
    <link href="https://example.gov/atom/1"/>
    <id>https://example.gov/atom/1</id>
    <summary>SEC announced charges against...</summary>
    <published>2026-09-18T15:00:00Z</published>
  </entry>
</feed>
"""


def _provider_with_transport(handler) -> RssFeedProvider:
    provider = RssFeedProvider()
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)
    return provider


@pytest.mark.asyncio
async def test_poll_parses_rss_items_in_order():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=RSS_XML, headers={"content-type": "application/rss+xml"})

    provider = _provider_with_transport(handler)
    entries = await provider.poll("https://example.gov/feed.rss")

    assert len(entries) == 2
    assert entries[0]["title"] == "Fed announces rate decision"
    assert entries[0]["link"] == "https://example.gov/press/1"
    assert entries[0]["guid"] == "https://example.gov/press/1"
    assert "Federal Reserve" in entries[0]["summary"]
    assert entries[0]["published_at"].year == 2026
    assert entries[0]["published_at"].month == 9
    assert entries[0]["published_at"].day == 18
    await provider.aclose()


@pytest.mark.asyncio
async def test_poll_parses_atom_entries():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=ATOM_XML)

    provider = _provider_with_transport(handler)
    entries = await provider.poll("https://example.gov/feed.atom")

    assert len(entries) == 1
    assert entries[0]["title"] == "SEC charges filed"
    assert entries[0]["link"] == "https://example.gov/atom/1"
    assert entries[0]["published_at"].tzinfo is not None
    assert entries[0]["published_at"].astimezone(timezone.utc).hour == 15
    await provider.aclose()


@pytest.mark.asyncio
async def test_poll_raises_on_malformed_xml():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<rss><channel><item><title>unclosed")

    provider = _provider_with_transport(handler)
    with pytest.raises(RssFeedError):
        await provider.poll("https://example.gov/bad.rss")
    await provider.aclose()


@pytest.mark.asyncio
async def test_poll_raises_on_unrecognized_root_element():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html><body>not a feed</body></html>")

    provider = _provider_with_transport(handler)
    with pytest.raises(RssFeedError):
        await provider.poll("https://example.gov/notafeed.html")
    await provider.aclose()


@pytest.mark.asyncio
async def test_poll_raises_on_non_retryable_http_error():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(404, text="not found")

    provider = _provider_with_transport(handler)
    with pytest.raises(RssFeedError):
        await provider.poll("https://example.gov/missing.rss")
    assert calls["n"] == 1
    await provider.aclose()


@pytest.mark.asyncio
async def test_poll_retries_then_succeeds():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 2:
            return httpx.Response(503, text="unavailable")
        return httpx.Response(200, content=RSS_XML)

    provider = _provider_with_transport(handler)
    entries = await provider.poll("https://example.gov/feed.rss")
    assert len(entries) == 2
    assert calls["n"] == 2
    await provider.aclose()


def test_missing_title_or_link_are_empty_strings_not_exceptions():
    from aegis.providers.rss.client import _parse_rss
    from xml.etree import ElementTree

    root = ElementTree.fromstring(
        b"<rss><channel><item><guid>x</guid></item></channel></rss>"
    )
    entries = _parse_rss(root)
    assert entries[0]["title"] == ""
    assert entries[0]["link"] == ""
    assert entries[0]["published_at"] is None
