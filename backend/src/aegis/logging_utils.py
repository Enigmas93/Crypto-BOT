"""Structured JSON logging (spec section 98).

Every log line is a single JSON object with a fixed set of fields. Secrets
must never be passed in `metadata` - callers are responsible for that, but
`configure_logging` also scrubs a small set of known-sensitive keys as a
last line of defense.
"""
from __future__ import annotations

import json
import logging
import sys
import time
from typing import Any

_SENSITIVE_KEYS = {"api_key", "api_secret", "secret", "password", "token", "signature"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": round(time.time(), 3),
            "service": getattr(record, "service", record.name),
            "level": record.levelname,
            "event": getattr(record, "event", record.getMessage()),
            "symbol": getattr(record, "symbol", None),
            "message": record.getMessage(),
            "metadata": _scrub(getattr(record, "metadata", {}) or {}),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def _scrub(metadata: dict[str, Any]) -> dict[str, Any]:
    return {
        k: ("***" if k.lower() in _SENSITIVE_KEYS else v)
        for k, v in metadata.items()
    }


def configure_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    root.setLevel(level.upper())
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)

    # httpx logs "HTTP Request: GET <full url incl. query string>" at INFO -
    # for FRED/BEA that URL includes api_key/UserID in plaintext (found live,
    # via this project's own monitoring, not a hypothetical). _scrub() above
    # only covers metadata dict keys passed to log_event, not a third-party
    # library's own log message text, so the fix has to live here: httpx's
    # request-line noise is redundant with this project's own log_event
    # calls anyway (every engine already logs what it did, with metadata
    # already scrubbed), so it's raised to WARNING rather than filtered
    # line-by-line - simpler, and it still surfaces real connection errors.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


class _ServiceLoggerAdapter(logging.LoggerAdapter):
    """Merges the adapter's fixed `extra` with per-call `extra`, instead of
    the stdlib default which silently discards per-call `extra`."""

    def process(self, msg, kwargs):
        merged = {**self.extra, **(kwargs.get("extra") or {})}
        kwargs["extra"] = merged
        return msg, kwargs


def get_logger(service: str) -> logging.LoggerAdapter:
    """Logger pre-tagged with a service name, used as the `service` field."""
    logger = logging.getLogger(service)
    return _ServiceLoggerAdapter(logger, {"service": service})


def log_event(
    logger: logging.LoggerAdapter,
    event: str,
    message: str = "",
    level: int = logging.INFO,
    symbol: str | None = None,
    **metadata: Any,
) -> None:
    logger.log(
        level,
        message or event,
        extra={"event": event, "symbol": symbol, "metadata": metadata},
    )
