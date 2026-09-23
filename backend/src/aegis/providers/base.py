"""Provider abstractions (spec sections 3 and 101).

Every external data source sits behind one of these interfaces so a free
source can later be swapped for a premium one without touching any engine
that consumes the data. Phase 1 only implements `MarketDataProvider`, via
Binance.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable
from typing import Any


class MarketDataProvider(ABC):
    """Contract for a source of market data: bootstrap history over REST,
    then stream live updates over a persistent connection."""

    @abstractmethod
    async def bootstrap_klines(self, symbol: str, interval: str, limit: int) -> list[dict[str, Any]]:
        """Fetch the last `limit` closed klines for (symbol, interval)."""

    @abstractmethod
    async def get_exchange_info(self) -> dict[str, Any]:
        """Fetch trading rules / symbol status for the whole exchange."""

    @abstractmethod
    async def stream(
        self,
        on_message: Callable[[dict[str, Any]], None],
        on_reconnect: Callable[[], None] | None = None,
    ) -> AsyncIterator[None]:
        """Run the live stream forever, invoking `on_message` per event and
        `on_reconnect` whenever the connection had to be re-established."""


class MacroDataProvider(ABC):
    """Placeholder contract for Phase 5 (FRED / BLS / BEA)."""

    @abstractmethod
    async def get_series(self, series_id: str) -> list[dict[str, Any]]: ...


class NewsProvider(ABC):
    """Contract for a source of news items - one feed URL in, the feed's
    current entries out. Parameterized by URL (not one provider per feed)
    the same way `MacroDataProvider.get_series` is parameterized by
    series_id: one provider instance serves every configured source."""

    @abstractmethod
    async def poll(self, feed_url: str) -> list[dict[str, Any]]: ...


class SentimentProvider(ABC):
    """Placeholder contract for the Fear & Greed index."""

    @abstractmethod
    async def get_current(self) -> dict[str, Any]: ...


class OnChainProvider(ABC):
    """Placeholder contract for optional future on-chain data sources."""

    @abstractmethod
    async def get_metric(self, name: str, asset: str) -> dict[str, Any] | None:
        """Returns None (never a fabricated value) when no reliable free
        source is available - callers must record DATA_UNAVAILABLE."""


class ExecutionProvider(ABC):
    """Placeholder contract for Phase 8/9 (Paper vs. Real execution)."""

    @abstractmethod
    async def place_order(self, order: dict[str, Any]) -> dict[str, Any]: ...

    @abstractmethod
    async def cancel_order(self, order_id: str, symbol: str) -> dict[str, Any]: ...
