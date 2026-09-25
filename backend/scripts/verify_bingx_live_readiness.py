#!/usr/bin/env python
"""Verifies that position sizing will actually work BEFORE trusting a real
BingX balance to this bot (Fase 17b) - read-only, places no orders.

Built after the user said they plan to deposit $100 real money into the
BingX account once satisfied it's safe. Two questions this answers that
nothing else in this codebase directly checks:

  1. Is the account's REAL equity (whatever mode - demo or live - is
     currently active) what the risk engine will actually size against?
     `RiskRepository.sync_equity_from_exchange` (wired into
     ShadowTradingEngine.sync_equity/MomentumTradingEngine.sync_equity,
     called once per poll cycle) keeps this true going forward - this
     script just shows the number.
  2. At that real equity, does `calculate_position_size` actually produce
     a valid, exchange-acceptable order for every symbol this bot trades,
     using REAL current prices/ATR and REAL BingX symbol rules
     (min_notional, step_size) - or does a small account (like a first
     $100 deposit) round down to zero quantity or fall below the
     exchange's minimum notional for some symbols? A silently-blocked
     entry ("ENTRY_BLOCKED" in the logs, forever, for one specific symbol)
     is easy to miss if nobody specifically checked for it in advance.

Usage (from backend/, venv active):
    python scripts/verify_bingx_live_readiness.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aegis.config import get_settings  # noqa: E402
from aegis.db.bingx_account_repository import BingxAccountRepository  # noqa: E402
from aegis.db.candle_repository import CandleRepository  # noqa: E402
from aegis.db.engine import close_pool, create_pool  # noqa: E402
from aegis.execution.bingx_provider import BingXExecutionProvider  # noqa: E402
from aegis.logging_utils import configure_logging, get_logger, log_event  # noqa: E402
from aegis.providers.bingx.rest_client import BingXFuturesRestClient, BingXRestError  # noqa: E402
from aegis.risk.sizing import calculate_position_size  # noqa: E402
from aegis.technical.service import compute_snapshot  # noqa: E402

_LOG = get_logger("scripts.verify_bingx_live_readiness")


async def _main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)

    pool = await create_pool(settings)
    candle_repo = CandleRepository(pool)
    account_repo = BingxAccountRepository(pool, settings.credential_encryption_key)

    try:
        state = await account_repo.get_settings()
        if not state.credentials_configured:
            log_event(_LOG, "no_credentials", level=40,
                       message="Nenhuma chave da BingX configurada ainda - salve uma pela aba Configurações")
            return

        api_key, api_secret = await account_repo.get_decrypted_credentials()
        rest = BingXFuturesRestClient(testnet=(state.mode == "demo"), api_key=api_key, api_secret=api_secret)
        try:
            execution = BingXExecutionProvider(rest)

            try:
                equity = await execution.get_equity()
            except BingXRestError as exc:
                log_event(_LOG, "balance_fetch_failed", level=40, error=str(exc))
                return

            log_event(_LOG, "real_equity", mode=state.mode, equity=round(equity, 4))

            rules_by_symbol = await rest.get_symbol_rules()
            blocked_symbols = []
            for symbol in settings.symbols:
                df = await candle_repo.fetch_ohlcv(symbol, "1h", limit=250, closed_only=True)
                rules = rules_by_symbol.get(symbol)
                if df.empty or rules is None:
                    log_event(_LOG, "symbol_skipped", level=30, symbol=symbol,
                               reason="no candles yet" if df.empty else "no symbol rules from BingX")
                    continue

                snapshot = compute_snapshot(symbol, "1h", df)
                if snapshot.close is None or snapshot.atr_14 is None:
                    log_event(_LOG, "symbol_skipped", level=30, symbol=symbol, reason="insufficient indicator history")
                    continue

                stop_distance = snapshot.atr_14 * 2.0  # matches ShadowTradingConfig/MomentumConfig's default stop_atr_multiple
                stop_price = snapshot.close - stop_distance
                result = calculate_position_size(equity, settings.risk_per_trade, snapshot.close, stop_price, rules)

                log_event(
                    _LOG, "sizing_check", symbol=symbol, price=round(snapshot.close, 6),
                    quantity=result.quantity, notional=round(result.notional, 4),
                    risk_amount=round(result.risk_amount, 4), status=result.status,
                )
                if result.status != "OK":
                    blocked_symbols.append((symbol, result.status))

            if blocked_symbols:
                log_event(
                    _LOG, "READINESS_CHECK_FAILED", level=40, equity=round(equity, 2), blocked_symbols=blocked_symbols,
                    message="Um ou mais símbolos não conseguem abrir posição no tamanho de conta atual - "
                            "considere aumentar risk_per_trade ou excluir esses símbolos antes de operar com dinheiro real.",
                )
            else:
                log_event(_LOG, "READINESS_CHECK_PASSED", equity=round(equity, 2), mode=state.mode,
                           message="Todos os símbolos configurados conseguem abrir posição no equity atual.")
        finally:
            await rest.aclose()
    finally:
        await close_pool(pool)


if __name__ == "__main__":
    asyncio.run(_main())
