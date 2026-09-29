"""Trend engine (Fase 24): runs the BTC+ETH daily trend strategy as a PAPER
book against real BingX production prices and funding.

Why paper and not the BingX demo/live account: Shadow BingX already trades
BTCUSDT/ETHUSDT on that same account in one-way mode, where each symbol has
ONE net position - a multi-week trend position would net against Shadow's
bracket trades and corrupt both engines' order tracking. Real execution
needs its own BingX sub-account (separate API key) and the user's explicit
go-ahead; nothing here ever sends an order.

Loop contract (`run_once`, called every minute):
  1. settle funding events published since the last watermark (longs pay
     positive funding, shorts receive), using BingX's own settlement mark;
  2. once per day, REBALANCE_DELAY after the 00:00 UTC daily close (so the
     00:00 funding is settled against the position that actually held
     through it), compute target weights from the closed daily bars and
     trade the paper book to them;
  3. persist the book, marks and an hourly equity point.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime, timedelta

from aegis.trend.book import Fill
from aegis.trend.strategy import LOOKBACKS, SHORT_SCALE, TREND_SYMBOLS, VOL_WINDOW, required_bars, target_weights

REBALANCE_DELAY = timedelta(minutes=5)
DAY = timedelta(days=1)


class TrendPaperEngine:
    def __init__(self, market, repo, account_id: str = "trend_paper", starting_equity: float = 1000.0,
                 symbols: tuple[str, ...] = TREND_SYMBOLS) -> None:
        self.market = market
        self.repo = repo
        self.account_id = account_id
        self.starting_equity = starting_equity
        self.symbols = symbols

    async def run_once(self, now: datetime | None = None) -> dict:
        now = now or datetime.now(UTC)
        now_ms = int(now.timestamp() * 1000)
        state = await self.repo.load(self.account_id, self.starting_equity, now_ms)

        closes: dict[str, list[float]] = {}
        marks: dict[str, float] = {}
        last_closed_open: datetime | None = None
        for symbol in self.symbols:
            bars = await self.market.get_klines(symbol, "1d", limit=required_bars() + 10)
            closed = [b for b in bars if b.close_time_ms < now_ms]
            if not closed:
                raise ValueError(f"no closed daily bars for {symbol}")
            closes[symbol] = [b.close for b in closed]
            marks[symbol] = bars[-1].close   # forming bar's close = last traded price
            bar_open = datetime.fromtimestamp(closed[-1].open_time_ms / 1000, tz=UTC)
            last_closed_open = bar_open if last_closed_open is None else min(last_closed_open, bar_open)

        # 1. funding
        # One watermark for the whole book, so it only advances to a time
        # EVERY held symbol has already published (BingX can publish BTC's
        # 08:00 event before ETH's; advancing on BTC alone would skip ETH's).
        funding: list[tuple[str, datetime, float]] = []
        held = [s for s, p in state.book.positions.items() if p.qty]
        if held:
            events = {s: [e for e in await self.market.get_funding_rate_history(s, start_ms=state.last_funding_ms + 1)
                          if state.last_funding_ms < e[0] <= now_ms] for s in held}
            common = min((ev[-1][0] if ev else state.last_funding_ms) for ev in events.values())
            for symbol, ev in events.items():
                for t_ms, rate, mark in ev:
                    if t_ms <= common:
                        amount = state.book.apply_funding(symbol, rate, mark or marks[symbol])
                        funding.append((symbol, datetime.fromtimestamp(t_ms / 1000, tz=UTC), amount))
            state.last_funding_ms = common
        else:
            # Flat: nothing owed. Jump to the latest settlement boundary
            # (00/08/16 UTC) so a later position never pays for hours it didn't hold.
            boundary = now.replace(minute=0, second=0, microsecond=0)
            boundary -= timedelta(hours=boundary.hour % 8)
            state.last_funding_ms = max(state.last_funding_ms, int(boundary.timestamp() * 1000))

        # 2. daily rebalance
        fills: list[Fill] = []
        signal_payload = None
        due = (last_closed_open is not None
               and (state.last_rebalance_bar is None or last_closed_open > state.last_rebalance_bar)
               and now >= last_closed_open + DAY + REBALANCE_DELAY)
        if due:
            signals = target_weights(closes)
            before = {s: p.qty for s, p in state.book.positions.items()}
            fills = state.book.rebalance({s: sig.weight for s, sig in signals.items()}, marks)
            for f in fills:
                if f.side_after != "FLAT" and f.side_after != f.side_before:
                    state.opened_at[f.symbol] = now
                elif f.side_after == "FLAT":
                    state.opened_at.pop(f.symbol, None)
            state.last_rebalance_bar = last_closed_open
            signal_payload = {
                "bar": last_closed_open.isoformat(), "computed_at": now.isoformat(),
                "lookbacks": list(LOOKBACKS), "short_scale": SHORT_SCALE, "vol_window": VOL_WINDOW,
                "symbols": {s: {**asdict(sig), "votes": {str(k): v for k, v in sig.votes.items()},
                                "close": closes[s][-1], "qty_before": before.get(s, 0.0)}
                            for s, sig in signals.items()},
            }

        await self.repo.save(self.account_id, state, marks, now, fills=fills, funding=funding, signal=signal_payload)
        return {
            "action": "REBALANCED" if due else "HOLD",
            "equity": state.book.equity(marks),
            "fills": fills,
            "funding": funding,
            "signal": signal_payload,
        }
