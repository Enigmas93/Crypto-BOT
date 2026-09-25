"""Regression test for a real production crash (Fase 17d, found via a
Telegram alert 2026-09-25): BinanceExecutionProvider and BingXExecutionProvider
each used to define their OWN `BracketOpenError` class with an identical
shape - two separate, unrelated classes that happened to share a name.
ShadowTradingEngine/MomentumTradingEngine only ever imported Binance's
copy, so `except BracketOpenError` there never matched the exception
BingXExecutionProvider actually raised on a real bracket failure (BingX
rejected a stop price, code 110411) - the position was correctly flattened,
but the uncaught exception then crashed the whole run_bingx_shadow_trading.py
process. Fixed by moving the class to aegis.execution.models and having
both providers re-export the SAME object - this test is the guardrail
against that ever silently regressing back into two look-alikes.
"""
from __future__ import annotations

from aegis.execution import binance_provider, bingx_provider, models


def test_bracket_open_error_is_the_same_class_everywhere():
    assert binance_provider.BracketOpenError is models.BracketOpenError
    assert bingx_provider.BracketOpenError is models.BracketOpenError
    assert binance_provider.BracketOpenError is bingx_provider.BracketOpenError


def test_bingx_bracket_open_error_is_catchable_as_the_binance_imported_name():
    # The exact failure mode that crashed the process: code importing
    # BracketOpenError from binance_provider (as both trading engines do)
    # must still catch an instance raised via the BingX provider.
    try:
        raise bingx_provider.BracketOpenError("bracket failed", flattened=True)
    except binance_provider.BracketOpenError as exc:
        assert exc.flattened is True
    else:
        raise AssertionError("bingx_provider.BracketOpenError was not caught via binance_provider's import")
