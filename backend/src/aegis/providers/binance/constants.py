"""Binance USDT-M Futures endpoints.

These are the stable, documented base URLs as of this writing. Re-verify
against https://developers.binance.com/docs/derivatives/usds-margined-futures
before relying on them in a new environment - never assume they are
unchanged (spec rule 151).
"""
from __future__ import annotations

REST_BASE_URL_PROD = "https://fapi.binance.com"
REST_BASE_URL_TESTNET = "https://testnet.binancefuture.com"

# The `/futures/data/*` market-statistics endpoints (open interest history,
# long/short ratios) are NOT implemented on Futures Testnet - it 301-redirects
# them to a marketing page. Confirmed by hitting it directly; there is no
# equivalent testnet dataset. These are public, unauthenticated, read-only
# endpoints with zero trading risk, so the REST client always calls them
# against production regardless of BINANCE_TESTNET - unlike every other
# endpoint, which respects the testnet flag.
STATS_BASE_URL = "https://fapi.binance.com"

WS_BASE_URL_PROD = "wss://fstream.binance.com/stream"
WS_BASE_URL_TESTNET = "wss://stream.binancefuture.com/stream"

# Endpoints that have NO WebSocket stream on Binance Futures and must be
# polled over REST (spec section 5/104) -------------------------------------
POLL_ONLY_ENDPOINTS = {
    "open_interest": "/fapi/v1/openInterest",
    "open_interest_hist": "/futures/data/openInterestHist",
    "global_long_short_ratio": "/futures/data/globalLongShortAccountRatio",
    "top_long_short_account_ratio": "/futures/data/topLongShortAccountRatio",
    "top_long_short_position_ratio": "/futures/data/topLongShortPositionRatio",
    "exchange_info": "/fapi/v1/exchangeInfo",
    "funding_rate_history": "/fapi/v1/fundingRate",
    "ticker_24hr": "/fapi/v1/ticker/24hr",
    "klines": "/fapi/v1/klines",
}

# Binance closes idle/aged connections; a stream socket lives at most 24h and
# must be proactively renewed well before that (spec section 6).
WS_MAX_CONNECTION_SECONDS = 23 * 60 * 60  # renew after 23h, before Binance's ~24h cap
WS_STALE_AFTER_SECONDS = 90  # no message at all in 90s => assume dead, reconnect

# Signed (account/trading) endpoints - require an API key with Futures
# trading permission, added in the Execution Engine phase (Phase 10).
# Stable v1/v2 paths as of this writing - re-verify before relying on them
# in a new environment (spec rule 151).
#
# "algo_order" handles STOP_MARKET/TAKE_PROFIT_MARKET/STOP/TAKE_PROFIT/
# TRAILING_STOP_MARKET - Binance migrated these conditional order types off
# the plain "order" endpoint to this dedicated one effective 2025-12-09
# (confirmed live: the old endpoint returns error -4120, "Order type not
# supported for this endpoint. Please use the Algo Order API endpoints
# instead." - caught by this project's own testnet smoke test, not assumed
# from documentation alone). MARKET orders are unaffected and still use
# "order".
SIGNED_ENDPOINTS = {
    "order": "/fapi/v1/order",
    "algo_order": "/fapi/v1/algoOrder",
    "account_balance": "/fapi/v2/balance",
    "position_risk": "/fapi/v2/positionRisk",
    "leverage": "/fapi/v1/leverage",
}
