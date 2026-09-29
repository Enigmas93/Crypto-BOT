#!/usr/bin/env python
"""Runs the Trend engine forever (Fase 24): BTC+ETH daily trend as a PAPER
book on real BingX production prices and funding. Never sends an order -
see aegis/trend/engine.py for why, and for what real execution requires.

Usage (from backend/, venv active):
    python scripts/run_trend_engine.py
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aegis.config import get_settings  # noqa: E402
from aegis.db.engine import close_pool, create_pool  # noqa: E402
from aegis.db.trend_repository import TrendRepository  # noqa: E402
from aegis.logging_utils import configure_logging, get_logger, log_event  # noqa: E402
from aegis.notifications.webpush import build_webpush_notifier, close_message, entry_message  # noqa: E402
from aegis.providers.bingx.rest_client import BingXFuturesRestClient, BingXRestError  # noqa: E402
from aegis.trend.engine import TrendPaperEngine  # noqa: E402

_LOG = get_logger("scripts.run_trend_engine")
ACCOUNT_ID = "trend_paper"
POLL_SECONDS = 60


async def _notify_fills(pusher, repo: TrendRepository, result: dict, opened_before: dict) -> None:
    for f in result["fills"]:
        if f.side_before == f.side_after:
            continue   # resize only
        if f.side_before != "FLAT":
            since = opened_before.get(f.symbol)
            pnl = await repo.position_net_pnl(ACCOUNT_ID, f.symbol, since) if since else f.realized_pnl - f.fee
            pusher.notify(close_message(ACCOUNT_ID, f.symbol, f.side_before, pnl, None, "REBALANCE"))
        if f.side_after != "FLAT":
            pusher.notify(entry_message(ACCOUNT_ID, f.symbol, f.side_after, f.price))


async def _main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    pool = await create_pool(settings)
    pusher = build_webpush_notifier(pool, settings)
    repo = TrendRepository(pool)
    market = BingXFuturesRestClient(testnet=False)  # public production data only, no credentials
    engine = TrendPaperEngine(market, repo, account_id=ACCOUNT_ID)
    log_event(_LOG, "started", account_id=ACCOUNT_ID, mode="PAPER", symbols=list(engine.symbols))
    try:
        while True:
            try:
                snapshot = await repo.summary(ACCOUNT_ID)
                opened_before = {}
                if snapshot:
                    opened_before = {p["symbol"]: datetime.fromisoformat(p["opened_at"])
                                     for p in snapshot["positions"] if p["opened_at"]}
                result = await engine.run_once()
            except (BingXRestError, ValueError) as exc:
                log_event(_LOG, "cycle_failed", level=30, error=str(exc))
            else:
                if result["funding"]:
                    log_event(_LOG, "funding_settled", events=[(s, t.isoformat(), round(a, 4))
                                                               for s, t, a in result["funding"]])
                if result["action"] == "REBALANCED":
                    log_event(
                        _LOG, "rebalanced", equity=round(result["equity"], 2),
                        weights={s: round(v["weight"], 3) for s, v in result["signal"]["symbols"].items()},
                        fills=[{"symbol": f.symbol, "qty": round(f.qty, 6), "price": f.price,
                                "side": f"{f.side_before}->{f.side_after}"} for f in result["fills"]],
                    )
                    await _notify_fills(pusher, repo, result, opened_before)
            await asyncio.sleep(POLL_SECONDS)
    finally:
        await pusher.drain()
        await market.aclose()
        await close_pool(pool)


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
