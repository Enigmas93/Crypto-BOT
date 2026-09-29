#!/usr/bin/env python
"""System watchdog: every minute, checks that market data is fresh and that
every trading engine evaluated the last closed candle of every symbol it
trades (see aegis.health.watchdog). Pushes a phone alert (and Telegram) when
something stops, a reminder every 3h while it persists, and a notice when it
recovers. Writes the latest status for the dashboard.

Usage (from backend/, venv active):
    python scripts/run_watchdog.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aegis.config import get_settings  # noqa: E402
from aegis.db.engine import close_pool, create_pool  # noqa: E402
from aegis.health.watchdog import AlertTracker, collect_inputs, evaluate_health, status_payload  # noqa: E402
from aegis.logging_utils import configure_logging, get_logger, log_event  # noqa: E402
from aegis.notifications.telegram import TelegramNotifier  # noqa: E402
from aegis.notifications.webpush import PushMessage, build_webpush_notifier  # noqa: E402

_LOG = get_logger("scripts.run_watchdog")
_CHECK_SECONDS = 60.0
_STARTUP_GRACE_SECONDS = 180.0  # let every engine finish its first cycle after a restart


async def _main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    pool = await create_pool(settings)
    pusher = build_webpush_notifier(pool, settings)
    telegram = TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id)
    tracker = AlertTracker()
    started = asyncio.get_event_loop().time()
    log_event(_LOG, "watchdog_started", push_enabled=pusher.enabled)
    try:
        while True:
            now = datetime.now(UTC)
            try:
                issues = evaluate_health(await collect_inputs(pool, settings, now))
                payload = status_payload(issues, now)
                async with pool.acquire() as conn:
                    await conn.execute(
                        "INSERT INTO system_health_state (id, checked_at, payload) VALUES (1, $1, $2::jsonb) "
                        "ON CONFLICT (id) DO UPDATE SET checked_at = EXCLUDED.checked_at, payload = EXCLUDED.payload",
                        now, json.dumps(payload),
                    )
                if asyncio.get_event_loop().time() - started >= _STARTUP_GRACE_SECONDS:
                    to_alert, recovered = tracker.update(issues, now)
                    for issue in to_alert:
                        log_event(_LOG, "health_alert", level=40, key=issue.key, detail=issue.detail)
                        await pusher.send(PushMessage(f"🩺 {issue.title}", issue.detail, f"health-{issue.key}", "/#risk"))
                        await telegram.send(f"🩺 {issue.title}\n{issue.detail}")
                    for title in recovered:
                        log_event(_LOG, "health_recovered", title=title)
                        await pusher.send(PushMessage("✅ Normalizado", f"{title} — resolvido.", f"health-ok-{title}", "/#risk"))
                        await telegram.send(f"✅ Normalizado: {title}")
            except Exception as exc:  # noqa: BLE001 - one bad check must not kill the watchdog
                log_event(_LOG, "watchdog_check_failed", level=40, error=str(exc)[:300])
            await asyncio.sleep(_CHECK_SECONDS)
    finally:
        await telegram.aclose()
        await close_pool(pool)


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
