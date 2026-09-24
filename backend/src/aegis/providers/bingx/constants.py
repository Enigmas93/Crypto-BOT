"""BingX USDT-M Perpetual Swap endpoints.

Verified live 2026-09-24 against `github.com/BingX-API/api-ai-skills` (the
BingX-maintained structured docs for AI coding assistants) and by hitting
the real VST (testnet) API directly - re-verify before relying on these in
a new environment (spec rule 151), same policy as the Binance constants.
"""
from __future__ import annotations

# "prod-live" is real money; "prod-vst" is BingX's "Virtual Simulated
# Trading" - the direct equivalent of Binance Futures Testnet. Each has a
# documented .pro fallback domain BingX's own reference client falls back
# to on a network/timeout error - not used here yet (single-domain client,
# matching this project's existing BinanceFuturesRestClient), but kept in
# a comment for the day retrying against the fallback is worth adding.
REST_BASE_URL_PROD = "https://open-api.bingx.com"  # fallback: https://open-api.bingx.pro
REST_BASE_URL_VST = "https://open-api-vst.bingx.com"  # fallback: https://open-api-vst.bingx.pro

WS_BASE_URL = "wss://open-api-swap.bingx.com/swap-market"

# Public, unauthenticated market-data endpoints - not used by this project
# (Binance remains the data source), kept here only because the execution
# provider needs "Get Contract Info" for symbol precision/lot rules.
PUBLIC_ENDPOINTS = {
    "contracts": "/openApi/swap/v2/quote/contracts",
    "server_time": "/openApi/swap/v2/server/time",
}

# Signed (account/trading) endpoints.
SIGNED_ENDPOINTS = {
    "order": "/openApi/swap/v2/trade/order",
    "balance": "/openApi/swap/v3/user/balance",
    "positions": "/openApi/swap/v2/user/positions",
    "leverage": "/openApi/swap/v2/trade/leverage",
    "position_mode": "/openApi/swap/v1/positionSide/dual",
    "cancel_all_after": "/openApi/swap/v2/trade/cancelAllAfter",
    "listen_key": "/openApi/user/auth/userDataStream",
    "apply_vst": "/openApi/swap/v2/trade/getVst",
}

# Required on every request, including public ones (per BingX's own AI-skill
# authentication reference) - a source identifier, not a secret.
SOURCE_KEY_HEADER = "X-SOURCE-KEY"
SOURCE_KEY_VALUE = "BX-AI-SKILL"
API_KEY_HEADER = "X-BX-APIKEY"

# BingX rejects a request whose signed `timestamp` differs from server time
# by more than `recvWindow` (max 5000ms, enforced server-side) - tighter
# than Binance's window. This machine's local clock was measured live
# ~7.5s behind BingX's server time (real drift, not hypothetical - it
# produced a genuine "timestamp is invalid" error during verification), so
# the client must sign with server-clock-relative time, not raw local time.
RECV_WINDOW_MS = 5000
# How often to re-fetch the server-time offset - clock drift changes slowly.
CLOCK_SYNC_INTERVAL_SECONDS = 300.0
