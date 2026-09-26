"""Momentum Engine data types - Fase 14 ("moonshot" scanner, user-scoped to
liquid pairs only per explicit choice - see momentum/engine.py docstring).

No fixed take-profit target: exits are a hard safety-net stop (in case the
trailing leg never gets a chance - e.g. a gap straight through both) plus a
TRAILING_STOP_MARKET that ratchets in the position's favor, letting a
genuinely strong move run instead of capping it at a pre-set R-multiple.
The trailing leg only arms once price has moved a certain amount in profit
- without that, Binance/BingX arm trailing from the entry price
immediately, so the very first tick of normal noise can trigger a loss
before the trade ever had a chance to run (found live: two real entries
both closed via TRAILING_STOP within under an hour, small losses, before
any real move developed). Until activation, the hard ATR stop is the only
protection - which is the intended trade-off, giving the position room to
breathe past entry-point noise.

Fase 17j: that activation/callback distance used to be one fixed pair of
percentages (1.5%/2.0%) for every candidate, on every exchange - found
live 2026-09-26, after unblocking BingX's Momentum scanner to see its real
speculative-tail liquidity, that BingX's own micro-cap candidates (e.g.
SIUSDT, USEPAIDUSDT, AGRIPPAUSDT - verified live against real 15m klines)
have a 15m ATR of 5-12% of price, 50-100x a major like BTC's ~0.1%. A
fixed 1.5% activation on a 12%-ATR token arms the trailing on barely a
tenth of one normal candle's range, and a fixed 2.0% callback then closes
it on ordinary noise almost immediately - never giving a genuine momentum
move room to develop, the exact failure this mechanism was built to avoid
in the first place. `trailing_activation_atr_multiple`/
`trailing_callback_atr_multiple` scale both distances by the position's
OWN symbol's real ATR% (same signal `stop_atr_multiple` already uses for
the hard stop), clamped to a sane [min, max] band so no single symbol's
distance goes unbounded in either direction. The multiples (1.0/1.33) are
chosen so a ~1.5% ATR candidate - the rough profile of Binance's own
already-validated momentum candidates (RARE/QUICK/ONE-tier) - lands close
to the old fixed 1.5%/2.0% defaults, preserving that already-working
behavior while adapting correctly at both volatility extremes.
`momentum_score` (from the Scanner) is carried through to the trade record
for later analysis - was a high-momentum entry more or less reliable than a
low-momentum one? Not answerable yet with zero real trades, but the field
exists so that question can be asked later without a schema change.

Fase 17k: the user noticed real entries kept landing on legs that already
"looked tired" - buying near the top of a pump instead of near its start.
Audited the 8 most recent real Momentum trades (Binance + BingX) against
each symbol's own ATR14 and found a clean, real pattern: the ONLY winner
(RAREUSDT, +$3.92) entered after a move of 2.45x its own ATR14 over the
prior 2h (8 bars @ 15m); every trade that entered deeper into an already-
extended move lost - ORBIOUSDT at 6.25x ATR, RAREUSDT at 8.65x and again
at 10.78x. The other losers (SIUSDT, ARKUSDT, USEPAIDUSDT, AEROUSDT) all
had LOW extension (0.4x-1.5x) - their losses were bad signals, a different
failure mode this filter isn't meant to fix. Root cause: `rank_by_momentum`
scores candidates by 24h price change (backward-looking - rewards whoever
already moved the most) and the strategy signals that confirm entry
(breakout, trend-pullback) are themselves lagging technical confirmations
- both layers compound to enter well after a move has started, not near
its beginning. `extension_atr_multiple` (default 3.0, validated against
the real data above - sits cleanly between the 2.45x winner and the 6.25x
first loser) rejects a candidate whose last `extension_lookback_bars`
(default 8, ~2h @ 15m) move already exceeds that many ATRs - "this leg
already ran too far to chase," independent of what the strategy signals
say. Does not touch Paper/Shadow (fixed symbol list, different profile);
scoped to Momentum only, where the user's complaint was specifically
about chasing scanner-picked altcoin legs.

The same audit checked volume for a second, independent confirmation:
the two worst-extended losers (RAREUSDT at 8.65x/10.78x ATR) also carried
an extreme volume z-score at entry (+33.6 and +12.1, i.e. 12-34 standard
deviations above their recent baseline) - a classic climax/blow-off
pattern, volume spiking as a move exhausts rather than as it begins. The
winner and the other, non-extension-related losers all sat in a much
calmer -1.4 to +1.6 band. `climax_volume_zscore` (default 8.0, chosen
with a wide margin below the 12.1 bad cases and above the 1.6 normal
ones) rejects on climax volume even for a candidate that wouldn't yet
trip the pure price-extension check - the two signals catch overlapping
but not identical cases (a huge volume spike compressed into fewer
candles could climax before the ATR-multiple math flags it). Set to
`None` to disable independently of `extension_lookback_bars`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from aegis.strategy.strategies import ALL_STRATEGY_IDS

Side = str  # "LONG" | "SHORT"


@dataclass(slots=True)
class MomentumConfig:
    interval: str = "15m"
    account_id: str = "momentum"
    fees_pct: float = 0.0004
    strategy_ids: tuple[str, ...] = ALL_STRATEGY_IDS
    strategy_weights: dict[str, float] | None = None
    confluence_threshold: float = 30.0
    stop_atr_multiple: float = 2.0  # hard safety-net stop, not the primary exit
    # Trailing activation/callback distances scale with the symbol's OWN
    # ATR% instead of one fixed percentage for every candidate (Fase 17j -
    # see module docstring). Exchange's allowed callback range: 0.1-10.
    trailing_activation_atr_multiple: float = 1.0
    trailing_callback_atr_multiple: float = 1.33
    trailing_activation_min_pct: float = 0.5
    trailing_activation_max_pct: float = 5.0
    trailing_callback_min_pct: float = 0.5
    trailing_callback_max_pct: float = 6.0
    # Rejects a candidate whose recent move already exceeds this many ATRs
    # over the last `extension_lookback_bars` candles (Fase 17k - see
    # module docstring) - "already too tired to chase." 0 disables the
    # filter entirely.
    extension_lookback_bars: int = 8
    extension_atr_multiple: float = 3.0
    # Independent climax-volume check (Fase 17k - see module docstring).
    # None disables it.
    climax_volume_zscore: float | None = 8.0
    leverage: int = 3
    warmup_bars: int = 210
    candle_limit: int = 500
    min_quote_volume: float = 500_000_000.0  # 24h USDT volume floor - "liquid pairs only"
    top_n: int = 5


@dataclass(slots=True)
class MomentumPosition:
    side: Side
    entry_time: datetime
    entry_price: float
    quantity: float
    stop_order_id: int
    trailing_order_id: int
    stop_price: float
    risk_amount: float
    confluence_score: float
    momentum_score: float
    reasons: list[str] = field(default_factory=list)


@dataclass(slots=True)
class MomentumTrade:
    account_id: str
    symbol: str
    side: Side
    entry_time: datetime
    entry_price: float
    exit_time: datetime
    exit_price: float
    exit_reason: str  # STOP | TRAILING_STOP
    quantity: float
    gross_pnl: float
    fees_paid: float
    net_pnl: float
    r_multiple: float
    confluence_score: float
    momentum_score: float
    reasons: list[str] = field(default_factory=list)
