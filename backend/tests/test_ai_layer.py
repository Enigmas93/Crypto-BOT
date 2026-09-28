from datetime import UTC, datetime, timedelta

import httpx

from aegis.ai.analysts import (NewsAnalysis, parse_market_brief, parse_news_analysis, parse_trade_review)
from aegis.ai.engine import AiEngine
from aegis.ai.nvidia_client import AiResult, NvidiaClient, extract_json_object
from aegis.radar.listings import (ListingEvent, parse_binance_articles, parse_okx_announcements,
                                  parse_upbit_notices)


# -- JSON extraction ----------------------------------------------------------
def test_extracts_plain_json():
    assert extract_json_object('{"a": 1}') == {"a": 1}


def test_extracts_last_object_after_reasoning_and_fences():
    text = 'Thinking... maybe {"draft": true}. Final:\n```json\n{"sentiment": "negative", "magnitude": 4}\n```'
    assert extract_json_object(text) == {"sentiment": "negative", "magnitude": 4}


def test_braces_inside_strings_do_not_break_extraction():
    assert extract_json_object('{"reason": "uses {curly} text", "x": 2}') == {"reason": "uses {curly} text", "x": 2}


def test_no_json_returns_none():
    assert extract_json_object("no json here") is None
    assert extract_json_object(None) is None


# -- validators -----------------------------------------------------------------
def test_news_analysis_is_clamped_and_whitelisted():
    a = parse_news_analysis({"sentiment": "NEGATIVE", "magnitude": 9, "confidence": 3, "event_type": "weird",
                             "assets": ["$btc", "eth", "not a ticker!", "BTC"], "summary_pt": "x" * 500})
    assert a.sentiment == "negative"
    assert a.magnitude == 5
    assert a.confidence == 1.0
    assert a.event_type == "OTHER"
    assert a.assets == ["BTC", "ETH"]
    assert len(a.summary_pt) == 300


def test_news_analysis_rejects_unknown_sentiment():
    assert parse_news_analysis({"sentiment": "bullish"}) is None
    assert parse_news_analysis("not a dict") is None


def test_market_brief_requires_valid_bias():
    assert parse_market_brief({"bias": "up"}) is None
    b = parse_market_brief({"bias": "short", "conviction": 150, "risk_level": "extreme", "key_points": ["a", "", "b"]})
    assert (b.bias, b.conviction, b.risk_level, b.key_points) == ("SHORT", 100, "NORMAL", ["a", "b"])


def test_trade_review_requires_verdict_and_lesson():
    assert parse_trade_review({"verdict": "GOOD_TRADE"}) is None
    r = parse_trade_review({"verdict": "bad_entry", "lesson_pt": "Entrou tarde."})
    assert (r.verdict, r.lesson_pt) == ("BAD_ENTRY", "Entrou tarde.")


# -- NVIDIA client fallback -------------------------------------------------------
async def test_client_falls_back_to_next_model_and_cools_down_the_failed_one():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        model = __import__("json").loads(request.content)["model"]
        calls.append(model)
        if model == "a":
            return httpx.Response(503, json={"error": "overloaded"})
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok": true}'}}]})

    http = httpx.AsyncClient(base_url="https://x", transport=httpx.MockTransport(handler))
    client = NvidiaClient("key", models=("a", "b"), http=http)
    first = await client.complete_json("s", "u")
    second = await client.complete_json("s", "u")
    assert first.model == "b" and first.data == {"ok": True}
    assert second.model == "b"
    assert calls == ["a", "b", "b"]  # "a" skipped while cooling down


async def test_client_without_key_is_disabled():
    assert await NvidiaClient("").complete_json("s", "u") is None


# -- listing radar parsers ----------------------------------------------------------
def test_binance_parser_keeps_crypto_listings_and_drops_stocks():
    ts = int(datetime(2026, 9, 24, 7, 30, tzinfo=UTC).timestamp() * 1000)
    events = parse_binance_articles([
        {"title": "Binance Will List Hyperliquid (HYPE) with Seed Tag Applied", "releaseDate": ts},
        {"title": "Binance Exchange Adds GoPro (GPROB) and Reddit (RDDTB) bStocks Trading Pairs", "releaseDate": ts},
        {"title": "Binance Futures Will Launch 1000CATUSDT and ZORAUSDT USDⓈ-Margined Perpetual Contracts",
         "releaseDate": ts},
        {"title": "Binance Futures Will Launch OURAUSDT USDⓈ-Margined Perpetual Contract Pre-IPO Trading",
         "releaseDate": ts},
    ])
    assert [(e.source, e.ticker) for e in events] == [
        ("BINANCE_SPOT", "HYPE"), ("BINANCE_FUTURES", "CAT"), ("BINANCE_FUTURES", "ZORA")]


def test_upbit_parser_flags_krw_market():
    events = parse_upbit_notices([
        {"title": "캐시캣(CASHCAT) 신규 거래지원 안내 (KRW, BTC, USDT 마켓)", "listed_at": "2026-09-28T14:27:04+09:00"},
        {"title": "인젝티브(INJ) 거래 유의 종목 지정 해제 안내", "listed_at": "2026-09-22T16:00:00+09:00"},
    ])
    assert [(e.source, e.ticker) for e in events] == [("UPBIT_KRW", "CASHCAT")]
    assert events[0].announced_at == datetime(2026, 9, 28, 5, 27, 4, tzinfo=UTC)


def test_okx_parser():
    events = parse_okx_announcements([
        {"title": "OKX to list XDP/USDT (Doppler Finance) for spot trading", "pTime": "1790560810799"},
        {"title": "OKX to support new USDC spot trading pairs", "pTime": "1790560810799"},
    ])
    assert [(e.source, e.ticker) for e in events] == [("OKX_SPOT", "XDP")]


# -- engine with fakes ------------------------------------------------------------------
class _FakeClient:
    enabled = True

    def __init__(self, data):
        self.data = data

    async def complete_json(self, system, user, max_tokens=600):
        return AiResult(data=self.data, model="fake", latency_s=0.1) if self.data is not None else None


class _FakeAiRepo:
    def __init__(self):
        self.news = [{"source_id": "s", "guid": "g", "title": "SEC rejects ETF", "summary": "", "source_name": "SEC"}]
        self.saved = []
        self.listings = set()
        self.insights = []

    async def fetch_news_pending_analysis(self, limit=10, max_age_hours=48):
        return self.news

    async def upsert_news_analysis(self, source_id, guid, model, a: NewsAnalysis):
        self.saved.append((source_id, guid, model, a.sentiment))

    async def insert_listing_event(self, source, ticker, title, announced_at, bingx_symbol, price):
        key = (source, ticker, announced_at)
        if key in self.listings:
            return False
        self.listings.add(key)
        return True


async def test_news_classification_persists_only_valid_answers():
    repo = _FakeAiRepo()
    assert await AiEngine(_FakeClient({"sentiment": "negative", "magnitude": 4}), repo).classify_pending_news() == 1
    assert repo.saved == [("s", "g", "fake", "negative")]
    repo2 = _FakeAiRepo()
    assert await AiEngine(_FakeClient({"sentiment": "moon"}), repo2).classify_pending_news() == 0
    assert repo2.saved == []


class _FakeRadar:
    def __init__(self, events):
        self.events = events

    async def fetch_all(self):
        return self.events


class _Ticker:
    def __init__(self, symbol, last_price):
        self.symbol, self.last_price = symbol, last_price


class _FakeBingx:
    async def get_24h_tickers(self):
        return [_Ticker("HYPEUSDT", 41.5)]


class _FakeNotifier:
    def __init__(self):
        self.sent = []

    async def send(self, text):
        self.sent.append(text)


async def test_listing_radar_dedupes_and_alerts_only_fresh_events():
    now = datetime.now(UTC)
    events = [ListingEvent("BINANCE_SPOT", "HYPE", "Binance Will List Hyperliquid (HYPE)", now - timedelta(minutes=5)),
              ListingEvent("UPBIT_KRW", "OLD", "old", now - timedelta(days=1))]
    repo, notifier = _FakeAiRepo(), _FakeNotifier()
    engine = AiEngine(_FakeClient(None), repo, radar=_FakeRadar(events), bingx_rest=_FakeBingx(), notifier=notifier)
    assert await engine.run_listing_radar() == 2
    assert await engine.run_listing_radar() == 0  # deduplicated
    assert len(notifier.sent) == 1 and "HYPEUSDT @ 41.5" in notifier.sent[0]
