#!/usr/bin/env python
"""Runs the AI layer forever: news classification, hourly market briefs,
closed-trade reviews (NVIDIA NIM) and the listing radar.

Never touches orders. Without NVIDIA_API_KEY the AI tasks are skipped and
only the listing radar runs.

Usage (from backend/, venv active):
    python scripts/run_ai_engine.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aegis.ai.engine import AiEngine  # noqa: E402
from aegis.ai.nvidia_client import NvidiaClient  # noqa: E402
from aegis.config import get_settings  # noqa: E402
from aegis.db.ai_repository import AiRepository  # noqa: E402
from aegis.db.candle_repository import CandleRepository  # noqa: E402
from aegis.db.derivatives_repository import DerivativesRepository  # noqa: E402
from aegis.db.engine import close_pool, create_pool  # noqa: E402
from aegis.db.news_repository import NewsRepository  # noqa: E402
from aegis.logging_utils import configure_logging, get_logger, log_event  # noqa: E402
from aegis.notifications.telegram import TelegramNotifier  # noqa: E402
from aegis.providers.bingx.rest_client import BingXFuturesRestClient  # noqa: E402
from aegis.radar.listings import ListingRadarClient  # noqa: E402

_LOG = get_logger("scripts.run_ai_engine")
_CYCLE_SECONDS = 60.0


async def _main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    pool = await create_pool(settings)
    client = NvidiaClient(settings.nvidia_api_key, models=tuple(settings.nvidia_model_list))
    notifier = TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id)
    radar = ListingRadarClient()
    bingx_public = BingXFuturesRestClient(testnet=False)
    engine = AiEngine(
        client, AiRepository(pool), candle_repo=CandleRepository(pool), news_repo=NewsRepository(pool),
        derivatives_repo=DerivativesRepository(pool), settings=settings, radar=radar, bingx_rest=bingx_public,
        notifier=notifier,
    )
    log_event(_LOG, "ai_engine_started", ai_enabled=client.enabled, models=list(client.models))
    try:
        while True:
            for name, task in (
                ("listing_radar", engine.run_listing_radar()),
                ("news", engine.classify_pending_news() if client.enabled else None),
                ("briefs", engine.write_briefs(settings.all_trading_symbols) if client.enabled else None),
                ("reviews", engine.review_closed_trades() if client.enabled else None),
            ):
                if task is None:
                    continue
                try:
                    count = await task
                    if count:
                        log_event(_LOG, "ai_task_done", task=name, count=count)
                except Exception as exc:  # noqa: BLE001 - one failing task must not stop the others
                    log_event(_LOG, "ai_task_failed", level=40, task=name, error=str(exc)[:300])
            await asyncio.sleep(_CYCLE_SECONDS)
    finally:
        await client.aclose()
        await radar.aclose()
        await bingx_public.aclose()
        await notifier.aclose()
        await close_pool(pool)


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
