"""
The Treasurer's profit engine: make the most money, not the fattest margin.

First principles. Per-job margin is the wrong objective. With finite compute per day,
total profit is maximized by taking the *set* of jobs that returns the most expected
profit for the budget — a knapsack. The optimal ordering for a budget constraint is by
**profit density** (expected profit per unit of compute), taking greedily until the
budget is spent. That's why "最小利润赚最高的钱" works: a thin-margin job with tiny cost
has huge density, so many thin jobs beat a few fat ones. Accept any positive-EV job and
let the portfolio pick the best set that fits.

Two pieces:
  - `select_portfolio` — given scored candidates and a budget, return the profit-
    maximizing subset (greedy by density; optimal for the fractional knapsack, a strong
    heuristic for 0/1).
  - `YieldTracker` — the reward system: record realized profit per (category×platform)
    lane, expose each lane's yield (profit per $ spent), and a bounded multiplier that
    feeds proven high-yield lanes back into task selection. Earn → learn → earn more.

Pure and deterministic; the tracker is also available as a process-global so the web
layer can record realized profit and the selection layer can read yields.
"""

from __future__ import annotations

from dataclasses import dataclass, field


def profit_density(ev_cents: float, cost_cents: float) -> float:
    """Expected profit per unit of compute spent. Higher = better use of the budget."""
    return float(ev_cents) / max(1.0, float(cost_cents))


@dataclass
class PortfolioItem:
    """A candidate job scored for selection."""

    id: str
    ev_cents: float          # expected profit (from the EV brain)
    cost_cents: int          # compute cost to attempt it
    meta: dict = field(default_factory=dict)


@dataclass
class PortfolioResult:
    taken: list[str]
    skipped: list[tuple[str, str]]      # (id, reason)
    total_ev_cents: float
    total_cost_cents: int
    budget_cents: int

    @property
    def roi(self) -> float:
        return self.total_ev_cents / self.total_cost_cents if self.total_cost_cents else 0.0

    def as_dict(self) -> dict:
        return {
            "taken": self.taken,
            "skipped": self.skipped,
            "total_ev_cents": round(self.total_ev_cents, 2),
            "total_cost_cents": self.total_cost_cents,
            "budget_cents": self.budget_cents,
            "roi": round(self.roi, 4),
            "count": len(self.taken),
        }


def select_portfolio(
    items: list[PortfolioItem],
    budget_cents: int,
    *,
    min_ev_cents: float = 1.0,
) -> PortfolioResult:
    """
    Choose the profit-maximizing set of jobs that fits `budget_cents` of compute.

    Drops non-positive / sub-`min_ev_cents` jobs, then takes remaining jobs greedily by
    profit density (highest first; ties broken by larger EV) until the budget can't fit
    the next one. `budget_cents <= 0` means no compute constraint (take every eligible
    job) — pure thin-margin volume mode.
    """
    taken: list[str] = []
    skipped: list[tuple[str, str]] = []
    total_ev = 0.0
    used = 0

    eligible = []
    for it in items:
        if it.ev_cents < min_ev_cents:
            skipped.append((it.id, f"EV {it.ev_cents:.0f}¢ below min {min_ev_cents:.0f}¢"))
        else:
            eligible.append(it)

    ordered = sorted(
        eligible,
        key=lambda it: (profit_density(it.ev_cents, it.cost_cents), it.ev_cents),
        reverse=True,
    )
    for it in ordered:
        if budget_cents > 0 and used + it.cost_cents > budget_cents:
            skipped.append((it.id, "over budget"))
            continue
        taken.append(it.id)
        used += it.cost_cents
        total_ev += it.ev_cents
    return PortfolioResult(taken, skipped, total_ev, used, budget_cents)


@dataclass
class YieldTracker:
    """
    Reward system: realized profit attribution per lane (category×platform).

    Records what each lane actually earns and what it cost, so selection can favor lanes
    that make money and back off lanes that lose it — a closed earn→learn→earn loop.
    """

    _profit: dict[str, float] = field(default_factory=dict)   # realized net profit (cents)
    _spend: dict[str, float] = field(default_factory=dict)    # realized compute spend (cents)
    _count: dict[str, int] = field(default_factory=dict)

    @staticmethod
    def lane(category: str, platform: str | None) -> str:
        return f"{(category or 'general').lower()}:{(platform or 'default').lower()}"

    def record(self, lane: str, *, revenue_cents: float, cost_cents: float) -> None:
        """Record one settled job: net profit = revenue − cost, plus the spend."""
        self._profit[lane] = self._profit.get(lane, 0.0) + (float(revenue_cents) - float(cost_cents))
        self._spend[lane] = self._spend.get(lane, 0.0) + max(0.0, float(cost_cents))
        self._count[lane] = self._count.get(lane, 0) + 1

    def profit_of(self, lane: str) -> float:
        return self._profit.get(lane, 0.0)

    def yield_of(self, lane: str) -> float:
        """Realized profit per $ of compute (ROI). 0 when the lane has no spend yet."""
        spend = self._spend.get(lane, 0.0)
        return self._profit.get(lane, 0.0) / spend if spend > 0 else 0.0

    def multiplier(self, lane: str, *, max_swing: float = 0.25, sensitivity: float = 0.1) -> float:
        """
        Bounded EV multiplier for a lane from its realized yield: proven profitable lanes
        get a gentle boost, money-losing lanes a gentle penalty. Unseen lanes -> 1.0
        (neutral), so the reward loop only acts on evidence.
        """
        if self._spend.get(lane, 0.0) <= 0 or self._count.get(lane, 0) < 1:
            return 1.0
        adj = max(-max_swing, min(max_swing, sensitivity * self.yield_of(lane)))
        return round(1.0 + adj, 4)

    def top_lanes(self, n: int = 5) -> list[tuple[str, float]]:
        """Lanes by realized profit, most profitable first."""
        return sorted(self._profit.items(), key=lambda kv: kv[1], reverse=True)[:max(0, n)]

    def snapshot(self) -> dict:
        return {
            lane: {
                "profit_cents": round(self._profit.get(lane, 0.0), 2),
                "spend_cents": round(self._spend.get(lane, 0.0), 2),
                "count": self._count.get(lane, 0),
                "yield": round(self.yield_of(lane), 4),
            }
            for lane in sorted(set(self._profit) | set(self._spend))
        }

    def reset(self) -> None:
        self._profit.clear()
        self._spend.clear()
        self._count.clear()


# Process-global reward ledger: the web layer records realized profit here on job
# completion; the selection layer reads lane multipliers from it.
_YIELD = YieldTracker()


def record_yield(category: str, platform: str | None, *, revenue_cents: float, cost_cents: float) -> None:
    _YIELD.record(YieldTracker.lane(category, platform), revenue_cents=revenue_cents, cost_cents=cost_cents)


def lane_multiplier(category: str, platform: str | None) -> float:
    return _YIELD.multiplier(YieldTracker.lane(category, platform))


def yield_snapshot() -> dict:
    return _YIELD.snapshot()


def reset_yield() -> None:
    _YIELD.reset()
