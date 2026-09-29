"""Paper perpetual-futures book for the trend engine: exact cash/position
accounting (average entry price, realized PnL on reduce/flip, taker fee +
slippage on every fill, funding debits/credits). Pure - persisted by
TrendRepository."""
from __future__ import annotations

from dataclasses import dataclass, field

TAKER_FEE = 0.0005     # BingX perpetual taker
SLIPPAGE = 0.0003      # conservative for BTC/ETH market orders at 00:00 UTC
MIN_REBALANCE_FRACTION = 0.02   # ignore target changes smaller than 2% of equity


@dataclass(slots=True)
class Position:
    qty: float = 0.0            # signed, base units
    entry_price: float = 0.0


@dataclass(slots=True)
class Fill:
    symbol: str
    qty: float                  # signed delta
    price: float                # executed, slippage included
    fee: float
    realized_pnl: float
    side_before: str
    side_after: str


@dataclass(slots=True)
class Book:
    cash: float
    positions: dict[str, Position] = field(default_factory=dict)

    def equity(self, prices: dict[str, float]) -> float:
        return self.cash + sum(p.qty * (prices[s] - p.entry_price) for s, p in self.positions.items() if p.qty)

    def apply_fill(self, symbol: str, dq: float, mid: float,
                   fee_rate: float = TAKER_FEE, slippage: float = SLIPPAGE) -> Fill:
        pos = self.positions.setdefault(symbol, Position())
        before = side_of(pos.qty)
        px = mid * (1 + slippage) if dq > 0 else mid * (1 - slippage)
        fee = abs(dq) * px * fee_rate
        realized = 0.0
        q = pos.qty
        if q == 0 or (q > 0) == (dq > 0):
            new_q = q + dq
            pos.entry_price = (q * pos.entry_price + dq * px) / new_q
        else:
            closed = min(abs(dq), abs(q)) * (1 if q > 0 else -1)
            realized = closed * (px - pos.entry_price)
            new_q = q + dq
            if abs(new_q) < 1e-12:
                new_q, pos.entry_price = 0.0, 0.0
            elif (new_q > 0) != (q > 0):
                pos.entry_price = px            # flipped through zero
        pos.qty = new_q
        self.cash += realized - fee
        return Fill(symbol, dq, px, fee, realized, before, side_of(new_q))

    def apply_funding(self, symbol: str, rate: float, mark: float) -> float:
        """Longs pay positive funding, shorts receive it. Returns the cash change."""
        pos = self.positions.get(symbol)
        if not pos or not pos.qty:
            return 0.0
        amount = -pos.qty * mark * rate
        self.cash += amount
        return amount

    def rebalance(self, weights: dict[str, float], prices: dict[str, float],
                  min_fraction: float = MIN_REBALANCE_FRACTION) -> list[Fill]:
        """Trade every symbol to weight * equity notional. Changes below
        `min_fraction` of equity are skipped (daily inverse-vol drift would
        otherwise churn fees) - except a side change or going flat, which
        always executes."""
        equity = self.equity(prices)
        fills = []
        for symbol, w in weights.items():
            pos = self.positions.get(symbol, Position())
            target_qty = w * equity / prices[symbol]
            dq = target_qty - pos.qty
            side_change = side_of(target_qty) != side_of(pos.qty)
            if abs(dq) * prices[symbol] < min_fraction * equity and not side_change:
                continue
            if abs(dq) < 1e-12:
                continue
            fills.append(self.apply_fill(symbol, dq, prices[symbol]))
        return fills


def side_of(qty: float) -> str:
    if qty > 1e-12:
        return "LONG"
    if qty < -1e-12:
        return "SHORT"
    return "FLAT"
