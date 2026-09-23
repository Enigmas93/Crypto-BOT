"""Binance Futures REST client - public market-data endpoints plus signed
account/trading endpoints (Phase 10 - Execution Engine).

Signed endpoints (order placement/cancellation, balance, position risk)
require `api_key`/`api_secret` with Futures trading permission. Placing an
order NEVER auto-retries on a transport error (unlike every read-only
endpoint here) - if a POST /order request's response is lost to a network
error, we cannot know whether Binance actually received and acted on it;
blindly retrying could submit a second, duplicate order. Callers that need
to recover from a lost response must explicitly query order status first,
never just resend.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import time
import uuid
from typing import Any
from urllib.parse import urlencode

import httpx

from aegis.logging_utils import get_logger, log_event
from aegis.providers.binance.constants import (
    POLL_ONLY_ENDPOINTS,
    REST_BASE_URL_PROD,
    REST_BASE_URL_TESTNET,
    SIGNED_ENDPOINTS,
    STATS_BASE_URL,
)
from aegis.providers.binance.models import (
    AlgoOrderResult,
    Kline,
    OpenInterest,
    OrderResult,
    PositionRisk,
    SymbolRules,
    TickerStats,
)
from aegis.utils.backoff import BackoffPolicy

_LOG = get_logger("binance.rest")

_RETRYABLE_STATUS = {418, 429, 500, 502, 503, 504}
MAX_RETRIES = 4


class BinanceRestError(RuntimeError):
    """Raised after retries are exhausted, or on a non-retryable 4xx."""


class BinanceOrderError(RuntimeError):
    """Raised for a failed order placement/cancellation - never retried
    automatically, see this module's docstring."""


class BinanceFuturesRestClient:
    def __init__(self, testnet: bool = True, timeout: float = 10.0, api_key: str = "", api_secret: str = "") -> None:
        base_url = REST_BASE_URL_TESTNET if testnet else REST_BASE_URL_PROD
        self.testnet = testnet
        self._api_key = api_key
        self._api_secret = api_secret
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "BinanceFuturesRestClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    # -- low level -----------------------------------------------------
    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        policy = BackoffPolicy(base_seconds=0.5, max_seconds=8.0)
        last_error: Exception | None = None
        for _ in range(MAX_RETRIES):
            try:
                response = await self._client.get(path, params=params)
            except httpx.TransportError as exc:
                last_error = exc
                log_event(_LOG, "rest_transport_error", path=path, error=str(exc), level=30)
            else:
                if response.status_code == 200:
                    return response.json()
                if response.status_code not in _RETRYABLE_STATUS:
                    raise BinanceRestError(
                        f"{path} -> HTTP {response.status_code}: {response.text[:300]}"
                    )
                last_error = BinanceRestError(f"{path} -> HTTP {response.status_code}")
                log_event(
                    _LOG, "rest_retryable_error", path=path,
                    status=response.status_code, level=30,
                )
            await asyncio.sleep(policy.next_delay())
        raise BinanceRestError(f"{path} failed after {MAX_RETRIES} attempts") from last_error

    def _require_credentials(self) -> None:
        if not self._api_key or not self._api_secret:
            raise BinanceOrderError(
                "signed endpoint called without api_key/api_secret - set BINANCE_API_KEY/"
                "BINANCE_API_SECRET (never hard-code them)"
            )

    def _sign(self, params: dict[str, Any]) -> dict[str, Any]:
        signed = dict(params)
        signed["timestamp"] = int(time.time() * 1000)
        # 10s, not Binance's usual 5s-default example: this machine's local
        # clock was measured live ~4.7s behind Binance's server time (real
        # clock drift, not a hypothetical) - a 5s window left almost no
        # margin for ordinary network latency on top of that and caused a
        # real -1021 "Timestamp... outside of the recvWindow" failure
        # during testnet validation. 10s comfortably covers that plus
        # jitter without being so loose it defeats the point of the window.
        signed.setdefault("recvWindow", 10000)
        query = urlencode(signed, doseq=True)
        signed["signature"] = hmac.new(self._api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        return signed

    async def _signed_get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """Safe to retry - a read-only signed endpoint."""
        self._require_credentials()
        policy = BackoffPolicy(base_seconds=0.5, max_seconds=8.0)
        last_error: Exception | None = None
        headers = {"X-MBX-APIKEY": self._api_key}
        for _ in range(MAX_RETRIES):
            signed_params = self._sign(params or {})
            try:
                response = await self._client.get(path, params=signed_params, headers=headers)
            except httpx.TransportError as exc:
                last_error = exc
                log_event(_LOG, "rest_transport_error", path=path, error=str(exc), level=30)
            else:
                if response.status_code == 200:
                    return response.json()
                if response.status_code not in _RETRYABLE_STATUS:
                    raise BinanceRestError(f"{path} -> HTTP {response.status_code}: {response.text[:300]}")
                last_error = BinanceRestError(f"{path} -> HTTP {response.status_code}")
                log_event(_LOG, "rest_retryable_error", path=path, status=response.status_code, level=30)
            await asyncio.sleep(policy.next_delay())
        raise BinanceRestError(f"{path} failed after {MAX_RETRIES} attempts") from last_error

    async def _signed_write(self, method: str, path: str, params: dict[str, Any]) -> Any:
        """NEVER auto-retried - see this module's docstring. One request,
        one attempt; the caller decides what to do about a lost response."""
        self._require_credentials()
        headers = {"X-MBX-APIKEY": self._api_key}
        signed_params = self._sign(params)
        try:
            response = await self._client.request(method, path, params=signed_params, headers=headers)
        except httpx.TransportError as exc:
            raise BinanceOrderError(
                f"{method} {path} - no response received (network error): {exc}. "
                "Query order status before retrying - do not resend blindly."
            ) from exc
        if response.status_code != 200:
            raise BinanceOrderError(f"{method} {path} -> HTTP {response.status_code}: {response.text[:500]}")
        return response.json()

    # -- public endpoints -------------------------------------------------
    async def get_exchange_info(self) -> dict[str, Any]:
        return await self._get(POLL_ONLY_ENDPOINTS["exchange_info"])

    async def get_symbol_rules(self) -> dict[str, SymbolRules]:
        """Parsed tickSize/stepSize/minNotional/precision per symbol
        (spec section 115) - never hard-code these."""
        info = await self.get_exchange_info()
        rules: dict[str, SymbolRules] = {}
        for sym in info.get("symbols", []):
            filters = {f["filterType"]: f for f in sym.get("filters", [])}
            price_filter = filters.get("PRICE_FILTER", {})
            lot_filter = filters.get("LOT_SIZE", {})
            notional_filter = filters.get("MIN_NOTIONAL", {}) or filters.get("NOTIONAL", {})
            rules[sym["symbol"]] = SymbolRules(
                symbol=sym["symbol"],
                status=sym.get("status", "UNKNOWN"),
                price_precision=int(sym.get("pricePrecision", 0)),
                quantity_precision=int(sym.get("quantityPrecision", 0)),
                tick_size=float(price_filter.get("tickSize", 0) or 0),
                step_size=float(lot_filter.get("stepSize", 0) or 0),
                min_notional=float(notional_filter.get("notional", 0) or 0),
                raw=sym,
            )
        return rules

    async def get_klines(self, symbol: str, interval: str, limit: int = 500) -> list[Kline]:
        raw = await self._get(
            POLL_ONLY_ENDPOINTS["klines"],
            params={"symbol": symbol, "interval": interval, "limit": limit},
        )
        return [Kline.from_rest_row(symbol, interval, row) for row in raw]

    async def get_open_interest(self, symbol: str) -> OpenInterest:
        raw = await self._get(POLL_ONLY_ENDPOINTS["open_interest"], params={"symbol": symbol})
        return OpenInterest.from_rest_payload(raw)

    async def get_open_interest_hist(
        self, symbol: str, period: str = "5m", limit: int = 30
    ) -> list[dict[str, Any]]:
        return await self._get(
            STATS_BASE_URL + POLL_ONLY_ENDPOINTS["open_interest_hist"],
            params={"symbol": symbol, "period": period, "limit": limit},
        )

    async def get_global_long_short_ratio(
        self, symbol: str, period: str = "5m", limit: int = 30
    ) -> list[dict[str, Any]]:
        return await self._get(
            STATS_BASE_URL + POLL_ONLY_ENDPOINTS["global_long_short_ratio"],
            params={"symbol": symbol, "period": period, "limit": limit},
        )

    async def get_top_long_short_account_ratio(
        self, symbol: str, period: str = "5m", limit: int = 30
    ) -> list[dict[str, Any]]:
        return await self._get(
            STATS_BASE_URL + POLL_ONLY_ENDPOINTS["top_long_short_account_ratio"],
            params={"symbol": symbol, "period": period, "limit": limit},
        )

    async def get_top_long_short_position_ratio(
        self, symbol: str, period: str = "5m", limit: int = 30
    ) -> list[dict[str, Any]]:
        return await self._get(
            STATS_BASE_URL + POLL_ONLY_ENDPOINTS["top_long_short_position_ratio"],
            params={"symbol": symbol, "period": period, "limit": limit},
        )

    async def get_funding_rate_history(
        self, symbol: str, limit: int = 100
    ) -> list[dict[str, Any]]:
        return await self._get(
            POLL_ONLY_ENDPOINTS["funding_rate_history"],
            params={"symbol": symbol, "limit": limit},
        )

    async def get_24h_tickers(self) -> list[TickerStats]:
        """All symbols' 24h rolling stats in one call (Fase 14 - Market
        Scanner) - public, no symbol param, no auth needed."""
        raw = await self._get(POLL_ONLY_ENDPOINTS["ticker_24hr"])
        return [TickerStats.from_rest_payload(row) for row in raw]

    # -- signed endpoints (Phase 10 - Execution Engine) --------------------
    @staticmethod
    def _fmt(value: float) -> str:
        """Fixed-point string, never scientific notation - Binance rejects
        `1e-05`-style values. Exchange-rule rounding (tick/step size) is the
        caller's job (aegis.risk.sizing.round_down_to_step) - this only
        avoids a formatting footgun, it does not enforce precision."""
        return f"{value:.8f}".rstrip("0").rstrip(".")

    async def get_account_balance(self) -> list[dict[str, Any]]:
        return await self._signed_get(SIGNED_ENDPOINTS["account_balance"])

    async def get_position_risk(self, symbol: str | None = None) -> list[PositionRisk]:
        params = {"symbol": symbol} if symbol else {}
        raw = await self._signed_get(SIGNED_ENDPOINTS["position_risk"], params=params)
        return [PositionRisk.from_rest_payload(p) for p in raw]

    async def set_leverage(self, symbol: str, leverage: int) -> dict[str, Any]:
        """Sets the REAL leverage Binance will use for this symbol - found
        live (Fase 14/15) that without this, a symbol simply keeps
        whatever leverage was last set on the account (or Binance's own
        per-symbol default, e.g. 20x on a fresh testnet account for
        SOLUSDT) - completely independent of `TradeProposal.leverage`, the
        value RiskEngine's liquidation-distance math actually assumes.
        Must be called before every entry, not just once at startup - the
        account-wide leverage for a symbol can be changed by anything
        (another process, manual UI action), so never assumed to still be
        correct from a previous call. Never auto-retried - see module
        docstring; a lost response here must not be treated as "probably
        worked so retry is safe" for a leverage-critical operation."""
        if not 1 <= leverage <= 125:
            raise ValueError(f"leverage must be in [1, 125], got {leverage}")
        params = {"symbol": symbol, "leverage": leverage}
        return await self._signed_write("POST", SIGNED_ENDPOINTS["leverage"], params)

    async def place_market_order(
        self, symbol: str, side: str, quantity: float,
        reduce_only: bool = False, client_order_id: str | None = None,
    ) -> OrderResult:
        """side: BUY (open/add LONG or close SHORT) | SELL (open/add SHORT
        or close LONG). Never auto-retried - see module docstring."""
        params = {
            "symbol": symbol, "side": side, "type": "MARKET", "quantity": self._fmt(quantity),
            "reduceOnly": "true" if reduce_only else "false",
            "newClientOrderId": client_order_id or f"aegis-{uuid.uuid4().hex[:20]}",
        }
        raw = await self._signed_write("POST", SIGNED_ENDPOINTS["order"], params)
        return OrderResult.from_rest_payload(raw)

    async def place_stop_market_order(
        self, symbol: str, side: str, stop_price: float, quantity: float | None = None,
        close_position: bool = False, client_order_id: str | None = None,
    ) -> AlgoOrderResult:
        """A protective stop-loss: `side` is the CLOSING side (opposite of
        the position). `close_position=True` closes the entire position
        when triggered (no `quantity` needed/allowed); otherwise `quantity`
        is required. Goes through the Algo Order endpoint - see
        SIGNED_ENDPOINTS["algo_order"]'s comment for why. Never
        auto-retried - see module docstring."""
        params: dict[str, Any] = {
            "algoType": "CONDITIONAL", "symbol": symbol, "side": side, "type": "STOP_MARKET",
            "triggerPrice": self._fmt(stop_price), "workingType": "MARK_PRICE",
            "clientAlgoId": client_order_id or f"aegis-{uuid.uuid4().hex[:20]}",
        }
        if close_position:
            params["closePosition"] = "true"
        else:
            if quantity is None:
                raise ValueError("quantity is required when close_position=False")
            params["quantity"] = self._fmt(quantity)
            params["reduceOnly"] = "true"
        raw = await self._signed_write("POST", SIGNED_ENDPOINTS["algo_order"], params)
        return AlgoOrderResult.from_rest_payload(raw)

    async def place_take_profit_market_order(
        self, symbol: str, side: str, stop_price: float, quantity: float | None = None,
        close_position: bool = False, client_order_id: str | None = None,
    ) -> AlgoOrderResult:
        """Same shape as `place_stop_market_order` but the opposite trigger
        direction (Binance's TAKE_PROFIT_MARKET type). Algo Order endpoint,
        never auto-retried."""
        params: dict[str, Any] = {
            "algoType": "CONDITIONAL", "symbol": symbol, "side": side, "type": "TAKE_PROFIT_MARKET",
            "triggerPrice": self._fmt(stop_price), "workingType": "MARK_PRICE",
            "clientAlgoId": client_order_id or f"aegis-{uuid.uuid4().hex[:20]}",
        }
        if close_position:
            params["closePosition"] = "true"
        else:
            if quantity is None:
                raise ValueError("quantity is required when close_position=False")
            params["quantity"] = self._fmt(quantity)
            params["reduceOnly"] = "true"
        raw = await self._signed_write("POST", SIGNED_ENDPOINTS["algo_order"], params)
        return AlgoOrderResult.from_rest_payload(raw)

    async def place_trailing_stop_order(
        self, symbol: str, side: str, callback_rate_pct: float, quantity: float,
        activation_price: float | None = None, client_order_id: str | None = None,
    ) -> AlgoOrderResult:
        """A stop that ratchets in the position's favor and never against it
        - Binance manages the trailing server-side (Algo Order type
        TRAILING_STOP_MARKET), so there's no need for this engine to poll
        price and re-place the order itself. `callback_rate_pct` is the
        trailing distance (Binance requires 0.1-10, i.e. 0.1%-10%).
        `activation_price` is optional - if omitted, Binance starts
        trailing from the current mark price immediately. `side` is the
        CLOSING side (opposite of the position), same convention as
        `place_stop_market_order`. Never auto-retried."""
        if not 0.1 <= callback_rate_pct <= 10:
            raise ValueError(f"callback_rate_pct must be in [0.1, 10], got {callback_rate_pct}")
        params: dict[str, Any] = {
            "algoType": "CONDITIONAL", "symbol": symbol, "side": side, "type": "TRAILING_STOP_MARKET",
            "callbackRate": str(callback_rate_pct), "quantity": self._fmt(quantity), "reduceOnly": "true",
            "workingType": "MARK_PRICE", "clientAlgoId": client_order_id or f"aegis-{uuid.uuid4().hex[:20]}",
        }
        if activation_price is not None:
            params["activationPrice"] = self._fmt(activation_price)
        raw = await self._signed_write("POST", SIGNED_ENDPOINTS["algo_order"], params)
        return AlgoOrderResult.from_rest_payload(raw)

    async def cancel_order(self, symbol: str, order_id: int) -> OrderResult:
        """Idempotent in effect: canceling an already-canceled/filled order
        raises BinanceOrderError (Binance returns "Unknown order sent") -
        callers that just want "make sure it's gone" should treat that
        specific error as success, not retry it. For a plain (MARKET/LIMIT)
        order only - use cancel_algo_order for a STOP_MARKET/
        TAKE_PROFIT_MARKET order."""
        params = {"symbol": symbol, "orderId": order_id}
        raw = await self._signed_write("DELETE", SIGNED_ENDPOINTS["order"], params)
        return OrderResult.from_rest_payload(raw)

    async def cancel_algo_order(self, symbol: str, algo_id: int) -> dict[str, Any]:
        """Same idempotent-in-effect behavior as `cancel_order`, for a
        STOP_MARKET/TAKE_PROFIT_MARKET order placed via the Algo Order
        endpoint. Returns the raw confirmation dict, NOT an AlgoOrderResult
        - verified live that DELETE /fapi/v1/algoOrder's response is just
        {"algoId", "clientAlgoId", "code", "msg"}, not the full order shape
        POST/GET return (no `symbol`, `status`, etc. to parse)."""
        params = {"symbol": symbol, "algoId": algo_id}
        return await self._signed_write("DELETE", SIGNED_ENDPOINTS["algo_order"], params)

    async def get_algo_order(self, symbol: str, algo_id: int) -> AlgoOrderResult:
        raw = await self._signed_get(SIGNED_ENDPOINTS["algo_order"], params={"symbol": symbol, "algoId": algo_id})
        return AlgoOrderResult.from_rest_payload(raw)

    async def get_order(self, symbol: str, order_id: int) -> OrderResult:
        raw = await self._signed_get(SIGNED_ENDPOINTS["order"], params={"symbol": symbol, "orderId": order_id})
        return OrderResult.from_rest_payload(raw)
