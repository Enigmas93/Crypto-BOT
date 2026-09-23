"""Exponential backoff with jitter, used by every reconnecting client.

Never assume a WebSocket connection stays open forever (spec section 6) -
every reconnect loop in this codebase goes through `BackoffPolicy`.
"""
from __future__ import annotations

import random
from dataclasses import dataclass


@dataclass
class BackoffPolicy:
    base_seconds: float = 1.0
    max_seconds: float = 60.0
    multiplier: float = 2.0
    jitter: float = 0.2  # +/- fraction of the computed delay

    def __post_init__(self) -> None:
        self._attempt = 0

    def next_delay(self) -> float:
        """Return the delay (seconds) to wait before the next attempt, and
        advance the internal attempt counter."""
        raw = min(self.base_seconds * (self.multiplier**self._attempt), self.max_seconds)
        self._attempt += 1
        spread = raw * self.jitter
        return max(0.0, raw + random.uniform(-spread, spread))

    def reset(self) -> None:
        self._attempt = 0

    @property
    def attempt(self) -> int:
        return self._attempt
