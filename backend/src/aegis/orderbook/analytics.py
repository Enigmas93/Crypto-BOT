"""Pure order-book math (spec section 30). Every function takes plain
price/quantity levels - `list[tuple[float, float]]`, best price first, the
shape `PartialDepthUpdate` already provides - and returns a float or None.
No I/O, no state, trivial to hand-verify.
"""
from __future__ import annotations

Level = tuple[float, float]  # (price, quantity)


def microprice(best_bid: float, best_bid_qty: float, best_ask: float, best_ask_qty: float) -> float | None:
    """Size-weighted mid: more resting size on one side pulls the price
    toward the *other* side (that's the side about to get consumed)."""
    total = best_bid_qty + best_ask_qty
    if total == 0:
        return None
    return (best_bid * best_ask_qty + best_ask * best_bid_qty) / total


def top_of_book_imbalance(best_bid_qty: float, best_ask_qty: float) -> float | None:
    total = best_bid_qty + best_ask_qty
    if total == 0:
        return None
    return (best_bid_qty - best_ask_qty) / total


def depth_notional(levels: list[Level]) -> float:
    return sum(price * qty for price, qty in levels)


def depth_imbalance(bid_notional: float, ask_notional: float) -> float | None:
    total = bid_notional + ask_notional
    if total == 0:
        return None
    return (bid_notional - ask_notional) / total


def book_pressure(bids: list[Level], asks: list[Level], decay: float = 0.85) -> float | None:
    """Like `depth_imbalance`, but levels closer to the top of book count
    more (`decay**i`) - a wall 15 levels deep says less about near-term
    pressure than the same size one level in."""
    n = min(len(bids), len(asks))
    if n == 0:
        return None
    weighted_diff = 0.0
    weighted_total = 0.0
    for i in range(n):
        weight = decay**i
        bid_qty, ask_qty = bids[i][1], asks[i][1]
        weighted_diff += (bid_qty - ask_qty) * weight
        weighted_total += (bid_qty + ask_qty) * weight
    if weighted_total == 0:
        return None
    return weighted_diff / weighted_total
