from aegis.providers.binance.models import TickerStats
from aegis.scanner.ranking import rank_by_momentum


def _ticker(symbol, pct, vol) -> TickerStats:
    return TickerStats(symbol=symbol, price_change_pct=pct, last_price=100.0, quote_volume=vol)


def test_ranks_by_absolute_price_change_descending():
    tickers = [
        _ticker("AUSDT", 2.0, 1_000_000_000),
        _ticker("BUSDT", -15.0, 1_000_000_000),
        _ticker("CUSDT", 5.0, 1_000_000_000),
    ]
    result = rank_by_momentum(tickers, min_quote_volume=100_000_000)
    assert [c.symbol for c in result] == ["BUSDT", "CUSDT", "AUSDT"]


def test_excludes_below_liquidity_floor():
    tickers = [
        _ticker("HOTUSDT", 50.0, 1_000_000),  # huge move, but thin
        _ticker("LIQUSDT", 3.0, 1_000_000_000),
    ]
    result = rank_by_momentum(tickers, min_quote_volume=100_000_000)
    assert [c.symbol for c in result] == ["LIQUSDT"]


def test_excludes_non_usdt_pairs():
    tickers = [_ticker("BTCBUSD", 10.0, 1_000_000_000), _ticker("ETHUSDT", 3.0, 1_000_000_000)]
    result = rank_by_momentum(tickers, min_quote_volume=100_000_000)
    assert [c.symbol for c in result] == ["ETHUSDT"]


def test_excludes_bingx_non_crypto_nnco_prefixed_instruments():
    # Regression (Fase 17e): BingX's tokenized commodity/FX-cross contracts
    # (gold, oil, wheat, XAU/EUR, ...) clear both the "USDT" suffix and the
    # liquidity floor like any real pair, but BingX rejects order placement
    # for them on a one-way-mode API account (code 101414) - this crashed
    # the whole momentum process before this filter existed (and before
    # Fase 17d's separate BracketOpenError fix).
    tickers = [
        _ticker("NCCOGOLD2USDUSDT", 0.0, 785_000_000),  # the real symbol that caused the crash
        _ticker("BTCUSDT", 3.0, 1_000_000_000),
    ]
    result = rank_by_momentum(tickers, min_quote_volume=100_000_000)
    assert [c.symbol for c in result] == ["BTCUSDT"]


def test_excludes_explicit_exclusion_set():
    tickers = [_ticker("BTCUSDT", 10.0, 1_000_000_000), _ticker("ETHUSDT", 3.0, 1_000_000_000)]
    result = rank_by_momentum(tickers, min_quote_volume=100_000_000, exclude={"BTCUSDT"})
    assert [c.symbol for c in result] == ["ETHUSDT"]


def test_top_n_limits_result_count():
    tickers = [_ticker(f"S{i}USDT", float(i), 1_000_000_000) for i in range(10)]
    result = rank_by_momentum(tickers, min_quote_volume=100_000_000, top_n=3)
    assert len(result) == 3
    assert [c.symbol for c in result] == ["S9USDT", "S8USDT", "S7USDT"]


def test_empty_tickers_gives_empty_result():
    assert rank_by_momentum([], min_quote_volume=100_000_000) == []


def test_momentum_score_is_absolute_value_of_price_change():
    tickers = [_ticker("DOWNUSDT", -8.0, 1_000_000_000)]
    result = rank_by_momentum(tickers, min_quote_volume=100_000_000)
    assert result[0].momentum_score == 8.0
    assert result[0].price_change_pct == -8.0
