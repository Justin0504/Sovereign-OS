"""
Dynamic bid pricing: win the most paid work at the thinnest profitable price.

On platforms where you name a price (reverse auctions, proposals), the money-maximizing
move under the "最小利润赚最高的钱" thesis is to bid the LOWEST price that still covers
cost plus a thin margin — maximizing win probability, then winning on volume. When the
poster advertises a ceiling (the most they'll pay), never bid below the profitability
floor and never above the ceiling; optionally undercut the ceiling to capture margin
when it's generous.

Pure and deterministic. `price_bid` returns the cents to bid, or None when the job
can't be done profitably even at the poster's ceiling (skip it).
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class BidQuote:
    bid_cents: int | None       # None => skip (unprofitable at any winnable price)
    floor_cents: int            # lowest profitable price (cost + thin margin)
    reason: str

    def as_dict(self) -> dict:
        return {"bid_cents": self.bid_cents, "floor_cents": self.floor_cents, "reason": self.reason}


def price_bid(
    cost_cents: int,
    *,
    min_margin_ratio: float = 0.15,
    reward_ceiling_cents: int | None = None,
    undercut_ratio: float = 0.0,
) -> BidQuote:
    """
    Compute a bid.

    - `floor` = cost × (1 + min_margin_ratio): the thinnest price we'll accept.
    - No ceiling: bid the floor (max win probability, still profitable — pure volume).
    - Ceiling given: if the floor exceeds it, the job can't profit → skip (bid None).
      Otherwise bid the floor by default; with `undercut_ratio > 0`, bid just under the
      ceiling (ceiling × (1 − undercut)) but never below the floor — capturing margin on
      generous postings while still undercutting a ceiling-pinned competitor.
    """
    cost_cents = max(0, int(cost_cents))
    floor = int(round(cost_cents * (1.0 + max(0.0, min_margin_ratio))))
    floor = max(floor, cost_cents + 1)  # always strictly above cost

    if reward_ceiling_cents is not None and reward_ceiling_cents > 0:
        if floor > reward_ceiling_cents:
            return BidQuote(None, floor, (
                f"skip: floor {floor}¢ (cost {cost_cents}¢ +{min_margin_ratio*100:.0f}%) "
                f"exceeds ceiling {reward_ceiling_cents}¢"
            ))
        if undercut_ratio > 0:
            target = int(round(reward_ceiling_cents * (1.0 - min(1.0, undercut_ratio))))
            bid = max(floor, min(target, reward_ceiling_cents))
            return BidQuote(bid, floor, (
                f"undercut: bid {bid}¢ under ceiling {reward_ceiling_cents}¢ (floor {floor}¢)"
            ))
        return BidQuote(floor, floor, f"volume: bid floor {floor}¢ (ceiling {reward_ceiling_cents}¢)")
    return BidQuote(floor, floor, f"volume: bid floor {floor}¢ (no ceiling)")


def recommended_bid_cents(cost_cents: int, reward_ceiling_cents: int | None = None) -> int | None:
    """
    Convenience wrapper reading defaults from env:
      SOVEREIGN_BID_MIN_MARGIN  (default 0.15)  thin margin over cost
      SOVEREIGN_BID_UNDERCUT    (default 0.0)   fraction under a known ceiling
    Returns the bid cents, or None to skip.
    """
    quote = price_bid(
        cost_cents,
        min_margin_ratio=_env_float("SOVEREIGN_BID_MIN_MARGIN", 0.15),
        reward_ceiling_cents=reward_ceiling_cents,
        undercut_ratio=_env_float("SOVEREIGN_BID_UNDERCUT", 0.0),
    )
    return quote.bid_cents


def _env_float(name: str, default: float) -> float:
    try:
        return float((os.getenv(name) or "").strip() or default)
    except ValueError:
        return default
