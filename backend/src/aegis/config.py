"""Central configuration, loaded from environment variables / .env.

Never hard-code secrets here. Every field is either a safe default or read
from the environment via pydantic-settings.
"""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Binance -----------------------------------------------------------
    binance_api_key: str = ""
    binance_api_secret: str = ""
    binance_testnet: bool = True

    # Safety defaults (spec section 141/142) -----------------------------
    live_trading: bool = False
    risk_per_trade: float = 0.005
    max_daily_loss: float = 0.02
    max_drawdown: float = 0.10

    # Collector -----------------------------------------------------------
    collector_symbols: str = "BTCUSDT,ETHUSDT"
    collector_intervals: str = "1m,5m,15m,1h,4h,1d"
    collector_kline_bootstrap_limit: int = 500

    # Database (Phase 2) ---------------------------------------------------
    # Empty = persistence disabled, collector falls back to log-only (Phase 1
    # behaviour). Default matches docker-compose.yml's default credentials.
    database_url: str = "postgresql://aegis:aegis_dev_password@localhost:5432/aegis"
    db_pool_min_size: int = 2
    db_pool_max_size: int = 10
    db_writer_batch_size: int = 200
    db_writer_flush_interval: float = 1.0
    db_writer_queue_max_size: int = 20_000

    # Derivatives Engine (Phase 4) -------------------------------------------
    derivatives_poll_interval_seconds: float = 60.0
    derivatives_hist_limit: int = 30  # buckets fetched per poll, enough for acceleration + z-score
    funding_zscore_lookback: int = 100

    # Liquidation Engine (Phase 4b) -------------------------------------------
    liquidation_window_seconds: int = 300  # bucket width for windowed stats (5m)
    liquidation_baseline_buckets: int = 20  # trailing buckets kept for z-score/acceleration
    liquidation_snapshot_interval_seconds: float = 60.0

    # Order Book Engine (Phase 4c) ---------------------------------------------
    orderbook_depth_levels: int = 20  # Binance partial-depth stream supports 5, 10 or 20
    orderbook_snapshot_interval_seconds: float = 15.0

    # Macro Engine (Phase 5) ---------------------------------------------------
    fred_api_key: str = ""
    macro_series_config_path: str = "macro_series.yaml"
    macro_poll_interval_seconds: float = 21_600.0  # 6h - macro data changes slowly
    macro_history_limit: int = 260  # observations kept per series for zscore/yoy math

    # Macro Engine / BLS (Phase 5b) ---------------------------------------------
    # Optional, unlike FRED_API_KEY: BLS Public Data API v2 works unregistered
    # at lower limits (25 queries/day, 10-year span) - a key raises that to
    # 500/day and 20 years. Register free at https://data.bls.gov/registrationEngine/
    bls_api_key: str = ""
    bls_history_years: int = 3

    # Macro Engine / BEA (Phase 5c) ---------------------------------------------
    # Mandatory, like FRED - no anonymous access. Free signup:
    # https://apps.bea.gov/API/signup/index.cfm
    bea_api_key: str = ""
    bea_years_back: int = 10

    # News Engine (Phase 6) -----------------------------------------------------
    news_sources_config_path: str = "news_sources.yaml"
    news_poll_interval_seconds: float = 900.0  # 15min - official press releases aren't high-frequency

    # News Engine / conflict detection (Phase 6b) --------------------------------
    news_conflict_window_hours: int = 24

    # Risk Engine (Phase 7) -------------------------------------------------------
    # risk_per_trade / max_daily_loss / max_drawdown already existed since Phase 1
    # (spec section 141/142 safety defaults) - the rest fill in the thresholds
    # those top-level limits need to actually be enforced by a state machine.
    drawdown_caution_pct: float = 0.05        # >= this fraction of peak equity lost -> CAUTION
    drawdown_reduced_risk_pct: float = 0.075  # >= this -> REDUCED_RISK (risk_per_trade cut)
    # max_drawdown (0.10 default) is the HALTED threshold - no new trades at all

    risk_reduction_factor_caution: float = 0.75      # CAUTION multiplies risk_per_trade by this
    risk_reduction_factor_reduced: float = 0.50      # REDUCED_RISK multiplies risk_per_trade by this
    risk_reduction_factor_loss_streak: float = 0.50  # applied by the loss-streak guard too

    loss_streak_cooldown_threshold: int = 3    # consecutive losses -> COOLDOWN (informational)
    loss_streak_reduce_risk_threshold: int = 5  # -> REDUCE_RISK (risk_per_trade cut)
    loss_streak_halt_threshold: int = 8         # -> HALT (no new trades)

    max_leverage: int = 5  # conservative default (spec section 111/142) - never a growth lever
    min_r_multiple: float = 1.5  # minimum acceptable reward:risk before BAD_RISK_REWARD
    max_open_positions: int = 10
    max_correlated_exposure_pct: float = 0.30

    # Liquidation-distance is an approximation (isolated margin, no tiered
    # maintenance-margin table yet - see risk/rules.py docstring) - kept
    # conservative on purpose until a real leverageBracket lookup replaces it.
    maintenance_margin_rate_estimate: float = 0.004
    liquidation_safety_margin: float = 0.75  # stop must sit within 75% of the estimated distance

    # Paper Trading (Phase 9) ----------------------------------------------------
    paper_account_id: str = "paper"
    paper_trading_poll_interval_seconds: float = 30.0
    paper_trading_candle_limit: int = 500

    # Speculative bucket (Fase 13) -------------------------------------------------
    # A separate, higher-volatility symbol set traded on a shorter interval
    # than core_trading_symbols - "quick return" opportunities the 1h core
    # loop would be too slow to catch. Not a full Core/Growth/Speculative
    # portfolio-allocation system (no target percentages, no rebalancing) -
    # just a second (symbols, interval) pair the same Paper/Shadow engines
    # already evaluate, exactly like the core one. `collector_symbols` must
    # include these too (or the collector never has candles for them) -
    # `all_symbols` below is the union callers should hand to the collector
    # and the other data engines.
    core_trading_symbols: str = "BTCUSDT,ETHUSDT"
    speculative_symbols: str = "SOLUSDT,XRPUSDT,DOGEUSDT,1000PEPEUSDT,NEARUSDT"
    speculative_interval: str = "15m"

    # Momentum / "moonshot" scanner (Fase 14) --------------------------------------
    # Scoped to liquid pairs ONLY - the user was explicitly asked and chose this
    # over including thin/newly-listed symbols, given the much higher risk
    # (manipulation, gap risk, delisting) of chasing the latter. No fixed
    # symbol list here - the Scanner (aegis.scanner.ranking) picks candidates
    # dynamically each cycle from Binance's real 24h stats.
    momentum_account_id: str = "momentum"
    momentum_interval: str = "15m"
    momentum_min_quote_volume: float = 500_000_000.0  # 24h USDT volume floor
    momentum_top_n: int = 5
    momentum_scan_interval_seconds: float = 900.0  # how often to re-rank candidates
    momentum_poll_interval_seconds: float = 30.0  # how often to check open positions / act on candidates
    momentum_trailing_callback_rate_pct: float = 2.0
    momentum_trailing_activation_pct: float = 1.5  # profit % before the trailing leg arms itself

    # Event Risk Engine / CoinMarketCal (Fase 15b) -------------------------------
    # Free tier, no anonymous access. Free signup: https://coinmarketcal.com/developer
    # Crypto-native events only (listings, mainnet launches, forks) - macro
    # events (FOMC/CPI/NFP) have no good free API, see backend/README.md
    # "Métricas da Fase 15b" for the (real, verified) research behind that.
    coinmarketcal_api_key: str = ""
    event_risk_poll_interval_seconds: float = 3600.0  # 1h - free tier is 3000 req/month, plenty of headroom
    # CoinMarketCal's own canonical slugs (verified live against the real
    # API 2026-09-23) for the assets behind our 7 traded symbols - not
    # derivable from the Binance symbol itself (e.g. "1000PEPEUSDT" -> "pepe",
    # not "1000pepe").
    event_risk_coin_slugs: str = "bitcoin,ethereum,solana,ripple,dogecoin,pepe,near-protocol"

    # Logging ---------------------------------------------------------------
    log_level: str = "INFO"

    @property
    def symbols(self) -> list[str]:
        return [s.strip().upper() for s in self.collector_symbols.split(",") if s.strip()]

    @property
    def intervals(self) -> list[str]:
        return [s.strip() for s in self.collector_intervals.split(",") if s.strip()]

    @property
    def core_symbols(self) -> list[str]:
        return [s.strip().upper() for s in self.core_trading_symbols.split(",") if s.strip()]

    @property
    def speculative_symbol_list(self) -> list[str]:
        return [s.strip().upper() for s in self.speculative_symbols.split(",") if s.strip()]

    @property
    def all_trading_symbols(self) -> list[str]:
        """Union of core + speculative, de-duplicated, order preserved -
        what `collector_symbols` should be set to for every data engine to
        see both buckets."""
        seen: dict[str, None] = {}
        for s in self.core_symbols + self.speculative_symbol_list:
            seen[s] = None
        return list(seen)

    @property
    def event_risk_coin_slug_list(self) -> list[str]:
        return [s.strip().lower() for s in self.event_risk_coin_slugs.split(",") if s.strip()]

    @property
    def derivatives_periods(self) -> list[str]:
        """Binance's OI/long-short-ratio endpoints don't support 1m - use
        whatever configured intervals they do support."""
        supported = {"5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d"}
        return [i for i in self.intervals if i in supported]


def get_settings() -> Settings:
    """Fresh settings read (cheap; call again to pick up .env changes in tests)."""
    return Settings()
