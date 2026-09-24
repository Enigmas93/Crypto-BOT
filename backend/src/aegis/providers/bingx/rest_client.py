"""BingX Perpetual Swap REST client (Fase 16 - execution migration).

Signed account/trading endpoints exist so real orders can be placed on
BingX from the same Shadow/Paper/Live pipeline everything else already
uses; Binance remains the data source for those (Shadow, core Momentum).
The two public market-data methods (`get_24h_tickers`, `get_klines`) exist
ONLY for the BingX Momentum scanner - found live that scanning Binance's
ticker universe to trade on BingX produces real candidate/listing
mismatches (BROCCOLI714USDT: ranked by Binance's scanner, does not exist
on BingX at all), so BingX's own Momentum account scans and reads candles
from BingX's own market, never Binance's, using the exact same
`TickerStats`/`Kline` shapes and ranking logic Binance's scanner already
uses - the response formats are close enough to identical that reusing
those dataclasses' own parsers was correct, just with `source="bingx"`
set explicitly afterward (their classmethods default to "binance").

Two things make this client structurally different from
`BinanceFuturesRestClient`, both discovered live (2026-09-24), not assumed
from docs alone:

1. BingX responses are ALWAYS HTTP 200 with a `{code, msg, data}` envelope -
   `code == 0` means success, any other value is an error, even for things
   an HTTP API would normally signal with a 4xx/5xx status. Error handling
   here dispatches on the envelope's `code`, not the HTTP status.
2. BingX enforces a max `recvWindow` of 5000ms (vs. Binance's much looser
   window) and this machine's local clock was measured ~7.5s behind
   BingX's server time - comfortably enough to make every signed request
   fail with code 109400 ("timestamp is invalid") using raw local time.
   Every signed request is timestamped against a periodically-refreshed
   server-clock offset instead (see `_ensure_clock_synced`).

Symbols are translated at the client boundary: callers use our canonical
form (`BTCUSDT`, matching Binance/the rest of this codebase) and this
client converts to/from BingX's hyphenated form (`BTC-USDT`) - verified
live against BingX's real contract list for all 7 symbols this project
trades, including the `1000PEPE-USDT` naming (same "1000x" convention
Binance uses for the same asset).

Like `BinanceFuturesRestClient`, a signed WRITE (order placement/
cancellation, leverage, position mode) is NEVER auto-retried - if the
response to a mutating request is lost to a network error, we cannot know
whether BingX actually received and acted on it; blindly retrying could
place a duplicate order. Signed READS (balance, position, order status)
are retried the same way public endpoints are.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import time
import uuid
from typing import Any

import httpx

from aegis.logging_utils import get_logger, log_event
from aegis.providers.bingx.constants import (
    API_KEY_HEADER,
    CLOCK_SYNC_INTERVAL_SECONDS,
    PUBLIC_ENDPOINTS,
    RECV_WINDOW_MS,
    REST_BASE_URL_PROD,
    REST_BASE_URL_VST,
    SIGNED_ENDPOINTS,
    SOURCE_KEY_HEADER,
    SOURCE_KEY_VALUE,
)
from aegis.providers.bingx.models import OrderResult, PositionRisk, SymbolRules
from aegis.providers.binance.models import Kline, TickerStats
from aegis.utils.backoff import BackoffPolicy

_LOG = get_logger("bingx.rest")

# BingX's kline endpoint returns only an open time per candle (see
# get_klines' docstring) - close time is derived from this plus the
# interval's real duration. "1M" has no fixed ms duration; approximated
# as 30 days since this project never actually requests it (momentum runs
# on 15m).
_INTERVAL_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000, "6h": 21_600_000,
    "8h": 28_800_000, "12h": 43_200_000, "1d": 86_400_000, "3d": 259_200_000,
    "1w": 604_800_000, "1M": 2_592_000_000,
}

MAX_RETRIES = 4
# BingX's own documented gateway codes for "retry later" conditions - a rate
# limit or a transient system-busy response, never a rejected/invalid
# request (those must surface immediately, not be silently retried away).
_RETRYABLE_CODES = {100410, 100500, 109500, 110500}


class BingXRestError(RuntimeError):
    """Raised after retries are exhausted, or on a non-retryable error code."""

    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


class BingXOrderError(RuntimeError):
    """Raised for a failed order placement/cancellation - never retried
    automatically, see this module's docstring."""

    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


def to_bingx_symbol(canonical_symbol: str) -> str:
    """BTCUSDT -> BTC-USDT. Only USDT-quoted pairs exist in this project;
    extend this if a non-USDT quote asset is ever traded."""
    if canonical_symbol.endswith("USDT") and "-" not in canonical_symbol:
        return f"{canonical_symbol[:-4]}-USDT"
    return canonical_symbol


def from_bingx_symbol(bingx_symbol: str) -> str:
    """BTC-USDT -> BTCUSDT."""
    return bingx_symbol.replace("-", "")


class BingXFuturesRestClient:
    def __init__(self, testnet: bool = True, timeout: float = 10.0, api_key: str = "", api_secret: str = "") -> None:
        base_url = REST_BASE_URL_VST if testnet else REST_BASE_URL_PROD
        self.testnet = testnet
        self._api_key = api_key
        self._api_secret = api_secret
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout)
        self._clock_offset_ms = 0
        self._clock_synced_at_monotonic: float | None = None

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "BingXFuturesRestClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    # -- clock sync --------------------------------------------------------
    async def _ensure_clock_synced(self) -> None:
        now_monotonic = time.monotonic()
        if (
            self._clock_synced_at_monotonic is not None
            and now_monotonic - self._clock_synced_at_monotonic < CLOCK_SYNC_INTERVAL_SECONDS
        ):
            return
        local_before = int(time.time() * 1000)
        try:
            response = await self._client.get(PUBLIC_ENDPOINTS["server_time"])
        except httpx.TransportError as exc:
            # A transient network blip here must surface as the same error
            # type every other call in this client raises - never a bare
            # httpx exception a caller's `except BingXRestError` wouldn't
            # catch (this is exactly the failure mode a supervisor/retry
            # loop needs to recognize as "transient, try again next cycle").
            raise BingXRestError(f"clock sync failed (network error): {exc}") from exc
        payload = response.json()
        server_time = int(payload["data"]["serverTime"])
        # Ignores round-trip latency (no local_after/2 correction) -
        # BingX's 5000ms recvWindow has enough slack for that either way;
        # correctness here only needs to be within seconds, not millis.
        self._clock_offset_ms = server_time - local_before
        self._clock_synced_at_monotonic = now_monotonic
        log_event(_LOG, "clock_synced", offset_ms=self._clock_offset_ms)

    def _synced_timestamp_ms(self) -> int:
        return int(time.time() * 1000) + self._clock_offset_ms

    # -- signing -------------------------------------------------------
    def _require_credentials(self) -> None:
        if not self._api_key or not self._api_secret:
            raise BingXOrderError(
                "signed endpoint called without api_key/api_secret - set BINGX_API_KEY/"
                "BINGX_API_SECRET (never hard-code them)"
            )

    @staticmethod
    def _canonical_str(value: Any) -> str:
        """Python's str() renders a bool as "True"/"False" - BingX's own
        reference client is JS/TS, where a boolean template-interpolates
        as lowercase "true"/"false", and the server reconstructs its own
        canonical string from the JSON body it received (where a real JSON
        boolean IS lowercase) before checking the signature. Sending a
        Python-cased "True" in the signing string produced a real, live
        code-100001 signature mismatch for `reduceOnly=True` even though
        the signature math itself was correct - `bool` must be checked
        before `int` since `isinstance(True, int)` is also True in Python."""
        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value)

    def _sign(self, params: dict[str, Any]) -> dict[str, Any]:
        unsorted = dict(params)
        unsorted["timestamp"] = self._synced_timestamp_ms()
        unsorted.setdefault("recvWindow", RECV_WINDOW_MS)
        # ASCII-sorted key=value, values NOT URL-encoded before signing -
        # BingX's own signing contract (references/authentication.md),
        # verified live 2026-09-24. Also builds the OUTGOING dict in this
        # same sorted order (not just the string used to compute the
        # signature) - found live that BingX's server does not re-sort
        # received parameters before checking the signature, so a request
        # transmitted in a different order than it was signed in fails
        # with code 100001 ("signature mismatch") even though the
        # signature itself was computed correctly.
        canonical = "&".join(f"{k}={self._canonical_str(unsorted[k])}" for k in sorted(unsorted))
        signature = hmac.new(self._api_secret.encode(), canonical.encode(), hashlib.sha256).hexdigest()
        signed = {k: unsorted[k] for k in sorted(unsorted)}
        signed["signature"] = signature
        return signed

    # -- low level: public / signed-read (retryable) ------------------------
    async def _get(self, path: str, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> Any:
        policy = BackoffPolicy(base_seconds=0.5, max_seconds=8.0)
        last_error: Exception | None = None
        request_headers = {SOURCE_KEY_HEADER: SOURCE_KEY_VALUE, **(headers or {})}
        for _ in range(MAX_RETRIES):
            try:
                response = await self._client.get(path, params=params, headers=request_headers)
            except httpx.TransportError as exc:
                last_error = exc
                log_event(_LOG, "rest_transport_error", path=path, error=str(exc), level=30)
            else:
                payload = response.json()
                code = payload.get("code", 0)
                if code == 0:
                    return payload["data"]
                if code not in _RETRYABLE_CODES:
                    raise BingXRestError(f"{path} -> code {code}: {payload.get('msg', '')}", code=code)
                last_error = BingXRestError(f"{path} -> code {code}: {payload.get('msg', '')}", code=code)
                log_event(_LOG, "rest_retryable_error", path=path, code=code, level=30)
            await asyncio.sleep(policy.next_delay())
        raise BingXRestError(f"{path} failed after {MAX_RETRIES} attempts") from last_error

    async def _signed_get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        self._require_credentials()
        await self._ensure_clock_synced()
        headers = {API_KEY_HEADER: self._api_key}
        policy = BackoffPolicy(base_seconds=0.5, max_seconds=8.0)
        last_error: Exception | None = None
        for _ in range(MAX_RETRIES):
            signed_params = self._sign(params or {})
            try:
                response = await self._client.get(
                    path, params=signed_params, headers={SOURCE_KEY_HEADER: SOURCE_KEY_VALUE, **headers},
                )
            except httpx.TransportError as exc:
                last_error = exc
                log_event(_LOG, "rest_transport_error", path=path, error=str(exc), level=30)
            else:
                payload = response.json()
                code = payload.get("code", 0)
                if code == 0:
                    return payload["data"]
                if code not in _RETRYABLE_CODES:
                    raise BingXRestError(f"{path} -> code {code}: {payload.get('msg', '')}", code=code)
                last_error = BingXRestError(f"{path} -> code {code}: {payload.get('msg', '')}", code=code)
                log_event(_LOG, "rest_retryable_error", path=path, code=code, level=30)
            await asyncio.sleep(policy.next_delay())
        raise BingXRestError(f"{path} failed after {MAX_RETRIES} attempts") from last_error

    # -- low level: signed writes (never auto-retried) -----------------------
    async def _signed_write_json(self, path: str, params: dict[str, Any]) -> Any:
        """POST with a JSON body - used for every mutating call. BingX
        accepts signed POSTs either as a query string or as a JSON body
        with `timestamp`/`signature` embedded in it; JSON body is used here
        unconditionally because it is the only transport BingX documents
        for parameters that are themselves JSON strings (the bracket
        order's `stopLoss`/`takeProfit`), and using it for every write
        keeps this client to one code path instead of two."""
        self._require_credentials()
        await self._ensure_clock_synced()
        signed = self._sign(params)
        headers = {API_KEY_HEADER: self._api_key, SOURCE_KEY_HEADER: SOURCE_KEY_VALUE}
        try:
            response = await self._client.post(path, json=signed, headers=headers)
        except httpx.TransportError as exc:
            raise BingXOrderError(
                f"POST {path} - no response received (network error): {exc}. "
                "Query order status before retrying - do not resend blindly."
            ) from exc
        payload = response.json()
        code = payload.get("code", 0)
        if code != 0:
            raise BingXOrderError(f"POST {path} -> code {code}: {payload.get('msg', '')}", code=code)
        return payload["data"]

    async def _signed_delete(self, path: str, params: dict[str, Any]) -> Any:
        self._require_credentials()
        await self._ensure_clock_synced()
        signed = self._sign(params)
        headers = {API_KEY_HEADER: self._api_key, SOURCE_KEY_HEADER: SOURCE_KEY_VALUE}
        try:
            response = await self._client.request("DELETE", path, params=signed, headers=headers)
        except httpx.TransportError as exc:
            raise BingXOrderError(
                f"DELETE {path} - no response received (network error): {exc}. "
                "Query order status before retrying - do not resend blindly."
            ) from exc
        payload = response.json()
        code = payload.get("code", 0)
        if code != 0:
            raise BingXOrderError(f"DELETE {path} -> code {code}: {payload.get('msg', '')}", code=code)
        return payload["data"]

    # -- public / read endpoints ---------------------------------------------
    async def get_contracts(self) -> list[dict[str, Any]]:
        return await self._get(PUBLIC_ENDPOINTS["contracts"])

    async def get_symbol_rules(self) -> dict[str, SymbolRules]:
        contracts = await self.get_contracts()
        rules: dict[str, SymbolRules] = {}
        for contract in contracts:
            canonical = from_bingx_symbol(contract["symbol"])
            rules[canonical] = SymbolRules.from_contract_payload(canonical, contract)
        return rules

    async def get_24h_tickers(self) -> list[TickerStats]:
        """All BingX contracts' 24h rolling stats in one call - for the
        BingX Momentum scanner ONLY (see module docstring for why it can't
        reuse Binance's scan). BingX's response uses the exact same field
        names Binance's does (`priceChangePercent`, `lastPrice`,
        `quoteVolume`), so `TickerStats.from_rest_payload` is reused as-is;
        `source` is corrected to "bingx" afterward since that classmethod
        always defaults it to "binance", and the symbol is normalized back
        to canonical form (BTC-USDT -> BTCUSDT) so the scanner's output is
        directly comparable/usable the same way Binance's is."""
        raw = await self._get(PUBLIC_ENDPOINTS["ticker_24hr"])
        tickers = []
        for row in raw:
            ticker = TickerStats.from_rest_payload(row)
            ticker.symbol = from_bingx_symbol(ticker.symbol)
            ticker.source = "bingx"
            tickers.append(ticker)
        return tickers

    async def get_klines(self, symbol: str, interval: str, limit: int = 500) -> list[Kline]:
        """For the BingX Momentum scanner ONLY (see module docstring).

        The reference doc describes the same positional-array row shape
        Binance uses - NOT what the real endpoint returns. Verified live
        2026-09-24: each row is an OBJECT `{open, high, low, close,
        volume, time}` - no close time, quote volume, trade count, or
        taker-buy fields at all, and rows come back NEWEST-FIRST
        (Binance's convention, and the rest of this codebase's, is
        oldest-first). `time` was confirmed live to be the OPEN time (it
        fell inside the current, still-forming candle's window compared
        against the server clock) - `close_time_ms` is derived from it
        (open + interval duration - 1ms, Binance's own convention) since
        BingX doesn't return one. The four fields BingX doesn't provide
        (quote_volume, trades, taker_buy_*) are set to 0 - a real "not
        available from this endpoint" value, not a fabricated one; the
        Strategy Engine's signals here only ever read OHLCV."""
        raw = await self._get(
            PUBLIC_ENDPOINTS["klines"], params={"symbol": to_bingx_symbol(symbol), "interval": interval, "limit": limit},
        )
        now_ms = int(time.time() * 1000)
        duration_ms = _INTERVAL_MS.get(interval, 60_000)
        klines = []
        for row in raw:
            open_time_ms = int(row["time"])
            close_time_ms = open_time_ms + duration_ms - 1
            klines.append(Kline(
                symbol=symbol, interval=interval, open_time_ms=open_time_ms, close_time_ms=close_time_ms,
                open=float(row["open"]), high=float(row["high"]), low=float(row["low"]), close=float(row["close"]),
                volume=float(row["volume"]), quote_volume=0.0, trades=0,
                taker_buy_base_volume=0.0, taker_buy_quote_volume=0.0,
                is_closed=close_time_ms < now_ms, source="bingx",
            ))
        klines.reverse()  # BingX returns newest-first; this codebase expects oldest-first throughout
        return klines

    # -- signed reads --------------------------------------------------------
    async def get_account_balance(self) -> list[dict[str, Any]]:
        return await self._signed_get(SIGNED_ENDPOINTS["balance"])

    async def get_position_risk(self, symbol: str | None = None) -> list[PositionRisk]:
        params = {"symbol": to_bingx_symbol(symbol)} if symbol else {}
        raw = await self._signed_get(SIGNED_ENDPOINTS["positions"], params=params)
        return [PositionRisk.from_rest_payload(from_bingx_symbol(p["symbol"]), p) for p in raw]

    async def get_position_mode(self) -> bool:
        """Returns True if the account is in Hedge (dual-side) mode.

        `dualSidePosition` comes back as the STRING "true"/"false" (verified
        live 2026-09-24), not a JSON boolean - `bool(data["dualSidePosition"])`
        would silently be True for the STRING "false" too (any non-empty
        Python string is truthy), which would have made this method report
        Hedge mode even on an account already correctly switched to
        one-way. Comparing against the literal string is deliberate, not
        a style choice."""
        data = await self._signed_get(SIGNED_ENDPOINTS["position_mode"])
        return str(data.get("dualSidePosition", "false")).lower() == "true"

    @staticmethod
    def _unwrap_order(data: Any) -> dict[str, Any]:
        """Every order endpoint's real response nests the order fields one
        level deeper than the reference doc's flat field table shows -
        `{"order": {...fields...}}`, not the fields directly in `data` -
        confirmed live 2026-09-24 against Place Order, Get Order, and
        (defensively assumed, same doc note "same fields as Place Order
        response") Cancel Order."""
        if isinstance(data, dict) and "order" in data:
            return data["order"]
        return data

    async def get_order(self, symbol: str, order_id: int) -> OrderResult:
        raw = await self._signed_get(
            SIGNED_ENDPOINTS["order"], params={"symbol": to_bingx_symbol(symbol), "orderId": order_id},
        )
        return OrderResult.from_rest_payload(symbol, self._unwrap_order(raw))

    # -- signed writes --------------------------------------------------------
    async def set_position_mode(self, hedge_mode: bool) -> None:
        """One-time account setup, not called per-trade: BingX refuses this
        call while any position or open order exists (error 104103), so it
        must only run against a flat, idle account - see
        `BingXExecutionProvider`'s docstring for why this project always
        runs BingX in one-way mode (hedge_mode=False)."""
        await self._signed_write_json(
            SIGNED_ENDPOINTS["position_mode"], {"dualSidePosition": "true" if hedge_mode else "false"},
        )

    async def set_leverage(self, symbol: str, leverage: int) -> dict[str, Any]:
        """`side="BOTH"` - always correct for one-way mode, which is the
        only mode this project runs BingX in (see
        `BingXExecutionProvider`'s docstring). Never auto-retried."""
        if not 1 <= leverage <= 125:
            raise ValueError(f"leverage must be in [1, 125], got {leverage}")
        params = {"symbol": to_bingx_symbol(symbol), "side": "BOTH", "leverage": leverage}
        return await self._signed_write_json(SIGNED_ENDPOINTS["leverage"], params)

    async def place_market_order(
        self, symbol: str, side: str, quantity: float, reduce_only: bool = False, client_order_id: str | None = None,
    ) -> OrderResult:
        """side: BUY (open/add LONG or close SHORT) | SELL (open/add SHORT
        or close LONG). `positionSide="BOTH"` - one-way mode only, see
        `BingXExecutionProvider`'s docstring. Never auto-retried."""
        params: dict[str, Any] = {
            "symbol": to_bingx_symbol(symbol), "side": side, "positionSide": "BOTH", "type": "MARKET",
            # A raw JSON number, NOT the fixed-point STRING Binance's API
            # wants - verified live 2026-09-24: BingX's Go backend rejects
            # a quantity/price sent as a string here with a generic
            # code-109400 "invalid parameters" (no field-specific message).
            # round() avoids Python's json encoder falling back to
            # scientific notation for a very small float, the same failure
            # mode `_fmt` originally existed to avoid for Binance's
            # string-typed params.
            "quantity": round(quantity, 8),
            "clientOrderId": client_order_id or f"aegis{uuid.uuid4().hex[:16]}",
        }
        if reduce_only:
            # A real JSON boolean, not the string "true" - see `_canonical_str`'s
            # docstring for why the STRING "True"/"true" distinction matters
            # for the signature too, not just this request's payload.
            params["reduceOnly"] = True
        raw = await self._signed_write_json(SIGNED_ENDPOINTS["order"], params)
        return OrderResult.from_rest_payload(symbol, self._unwrap_order(raw))

    async def place_stop_market_order(
        self, symbol: str, side: str, stop_price: float, quantity: float, client_order_id: str | None = None,
    ) -> OrderResult:
        """A protective stop-loss: `side` is the CLOSING side (opposite of
        the position). Unlike Binance (which moved these to a dedicated
        Algo Order endpoint in late 2025), BingX places STOP_MARKET through
        the SAME order endpoint as a plain MARKET order - one call shape
        for everything, confirmed against the real "Place Order" reference.

        BingX also supports attaching `stopLoss`/`takeProfit` directly on
        the entry order as nested JSON params (a genuine single-call
        bracket, which Binance has no equivalent for) - NOT used here: the
        Place Order reference does not document how to retrieve the
        auto-created stop/take-profit legs' own order ids afterward, and
        this engine needs to track and query each leg independently to
        know which one filled (`ShadowTradingEngine._handle_open_position`).
        Rather than guess at an undocumented lookup, this places the two
        legs as separate, explicitly-tracked orders instead - the same
        proven 3-call pattern `BinanceExecutionProvider` already uses,
        which guarantees a distinct, known order id for each leg. Never
        auto-retried."""
        params: dict[str, Any] = {
            "symbol": to_bingx_symbol(symbol), "side": side, "positionSide": "BOTH", "type": "STOP_MARKET",
            "stopPrice": round(stop_price, 8), "quantity": round(quantity, 8), "reduceOnly": True,
            "workingType": "MARK_PRICE", "clientOrderId": client_order_id or f"aegis{uuid.uuid4().hex[:16]}",
        }
        raw = await self._signed_write_json(SIGNED_ENDPOINTS["order"], params)
        return OrderResult.from_rest_payload(symbol, self._unwrap_order(raw))

    async def place_take_profit_market_order(
        self, symbol: str, side: str, stop_price: float, quantity: float, client_order_id: str | None = None,
    ) -> OrderResult:
        """Same shape as `place_stop_market_order` but BingX's
        TAKE_PROFIT_MARKET type - see that method's docstring for why this
        is a separate call rather than the attached `takeProfit` param."""
        params: dict[str, Any] = {
            "symbol": to_bingx_symbol(symbol), "side": side, "positionSide": "BOTH", "type": "TAKE_PROFIT_MARKET",
            "stopPrice": round(stop_price, 8), "quantity": round(quantity, 8), "reduceOnly": True,
            "workingType": "MARK_PRICE", "clientOrderId": client_order_id or f"aegis{uuid.uuid4().hex[:16]}",
        }
        raw = await self._signed_write_json(SIGNED_ENDPOINTS["order"], params)
        return OrderResult.from_rest_payload(symbol, self._unwrap_order(raw))

    async def place_trailing_stop_order(
        self, symbol: str, side: str, callback_rate_pct: float, quantity: float,
        activation_price: float | None = None, client_order_id: str | None = None,
    ) -> OrderResult:
        """A stop that ratchets in the position's favor - BingX manages the
        trailing server-side (order type TRAILING_STOP_MARKET), same as
        Binance. `side` is the CLOSING side (opposite of the position),
        same convention as `place_stop_market_order`.

        `callback_rate_pct` uses the SAME convention as
        `BinanceFuturesRestClient.place_trailing_stop_order` (a percentage,
        0.1-10 meaning 0.1%-10%) for callers - internally converted to
        BingX's own `priceRate`, which is a FRACTION (0.05 = 5%, max 1) -
        a real, confirmed difference from Binance's `callbackRate`
        (already a percentage number like "2.0"). Never auto-retried."""
        if not 0.1 <= callback_rate_pct <= 10:
            raise ValueError(f"callback_rate_pct must be in [0.1, 10], got {callback_rate_pct}")
        params: dict[str, Any] = {
            "symbol": to_bingx_symbol(symbol), "side": side, "positionSide": "BOTH", "type": "TRAILING_STOP_MARKET",
            "priceRate": round(callback_rate_pct / 100, 6), "quantity": round(quantity, 8), "reduceOnly": True,
            "workingType": "MARK_PRICE", "clientOrderId": client_order_id or f"aegis{uuid.uuid4().hex[:16]}",
        }
        if activation_price is not None:
            params["activationPrice"] = round(activation_price, 8)
        raw = await self._signed_write_json(SIGNED_ENDPOINTS["order"], params)
        return OrderResult.from_rest_payload(symbol, self._unwrap_order(raw))

    async def cancel_order(self, symbol: str, order_id: int) -> OrderResult:
        """Idempotent in effect: canceling an already-filled/canceled order
        raises BingXOrderError with code 109421 - callers that just want
        "make sure it's gone" should treat that specific code as success."""
        raw = await self._signed_delete(
            SIGNED_ENDPOINTS["order"], {"symbol": to_bingx_symbol(symbol), "orderId": order_id},
        )
        return OrderResult.from_rest_payload(symbol, self._unwrap_order(raw))

    async def cancel_all_after(self, timeout_seconds: int) -> dict[str, Any]:
        """Native exchange-side dead-man's-switch: if not renewed within
        `timeout_seconds` (10-120s), BingX cancels every open order on this
        account by itself - a safety net Binance's API has no equivalent
        for. Never auto-retried (a write)."""
        if not 10 <= timeout_seconds <= 120:
            raise ValueError(f"timeout_seconds must be in [10, 120], got {timeout_seconds}")
        return await self._signed_write_json(
            SIGNED_ENDPOINTS["cancel_all_after"], {"type": "ACTIVATE", "timeOut": timeout_seconds},
        )

    async def cancel_all_after_close(self) -> dict[str, Any]:
        return await self._signed_write_json(SIGNED_ENDPOINTS["cancel_all_after"], {"type": "CLOSE", "timeOut": 10})

    async def apply_vst(self, amount: int, increase: bool = True) -> str:
        """VST-only: tops up (or reduces) this testnet account's virtual
        balance on demand - has no live/real-money equivalent. Returns the
        updated balance as BingX reports it.

        `adjustType`/`amount` are sent with the OPPOSITE types the
        reference doc's parameter table claims (`adjustType` an integer,
        not the documented string; `amount` a string, not the documented
        int64) - verified live 2026-09-24 by hitting the real endpoint:
        each mismatch fails with code 109500 ("json: cannot unmarshal ...
        of type ..."), a real doc-vs-server mismatch, not a hypothetical."""
        data = await self._signed_write_json(
            SIGNED_ENDPOINTS["apply_vst"], {"adjustType": 0 if increase else 1, "amount": str(int(amount))},
        )
        return str(data.get("balance", ""))
