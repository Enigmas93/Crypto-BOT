"""Tests for Settings' symbol-list parsing properties (Fase 13 speculative
bucket) - pure string parsing, no I/O, no env file needed."""
from __future__ import annotations

from aegis.config import Settings


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


def test_core_symbols_parses_and_uppercases():
    settings = _settings(core_trading_symbols="btcusdt, ethusdt")
    assert settings.core_symbols == ["BTCUSDT", "ETHUSDT"]


def test_speculative_symbol_list_parses_and_uppercases():
    settings = _settings(speculative_symbols="solusdt,xrpusdt")
    assert settings.speculative_symbol_list == ["SOLUSDT", "XRPUSDT"]


def test_speculative_symbol_list_empty_string_gives_empty_list():
    settings = _settings(speculative_symbols="")
    assert settings.speculative_symbol_list == []


def test_all_trading_symbols_is_the_union_preserving_order():
    settings = _settings(core_trading_symbols="BTCUSDT,ETHUSDT", speculative_symbols="SOLUSDT,XRPUSDT")
    assert settings.all_trading_symbols == ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]


def test_all_trading_symbols_deduplicates_overlap():
    settings = _settings(core_trading_symbols="BTCUSDT,ETHUSDT", speculative_symbols="ETHUSDT,SOLUSDT")
    assert settings.all_trading_symbols == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]


def test_defaults_match_env_example():
    settings = _settings()
    assert settings.core_symbols == ["BTCUSDT", "ETHUSDT"]
    assert settings.speculative_symbol_list == ["SOLUSDT", "XRPUSDT", "DOGEUSDT", "1000PEPEUSDT", "NEARUSDT"]
    assert settings.speculative_interval == "15m"


def test_event_risk_coin_slug_list_parses_and_lowercases():
    settings = _settings(event_risk_coin_slugs="Bitcoin, Ethereum")
    assert settings.event_risk_coin_slug_list == ["bitcoin", "ethereum"]


def test_event_risk_coin_slug_list_default_covers_all_traded_symbols():
    settings = _settings()
    assert settings.event_risk_coin_slug_list == [
        "bitcoin", "ethereum", "solana", "ripple", "dogecoin", "pepe", "near-protocol",
    ]
