"""
Capital allocator: split the day's compute budget across lanes by proven ROI.

The portfolio picks the best jobs *within* a budget; this decides how much budget each
lane (category×platform) should get in the first place. First principles: put money
where it earns the most — allocate in proportion to each lane's realized yield (profit
per $ spent) — while always reserving an exploration slice so new or recovering lanes
still get tried. That's the explore/exploit split that keeps a portfolio both greedy on
today's winners and open to tomorrow's.

Composes the three financial signals already in place: `YieldTracker` supplies realized
yields, `cost_model` makes those numbers accurate, and `select_portfolio` then spends
each lane's allocation. Pure and deterministic.
"""

from __future__ import annotations


def allocate_budget(
    total_cents: int,
    lane_yields: dict[str, float],
    *,
    exploration_frac: float = 0.2,
) -> dict[str, int]:
    """
    Divide `total_cents` of compute budget across the given lanes.

    - An **exploration** slice (`exploration_frac` of the total) is split evenly across
      all lanes, so no lane is ever starved to zero and discovery continues.
    - The **exploit** remainder is split in proportion to each lane's positive yield, so
      proven-profitable lanes get the most. If no lane has positive yield yet, the whole
      budget becomes exploration (spread evenly) — sensible cold-start.

    Returns {lane: cents}. `total_cents <= 0` or no lanes -> {}.
    """
    total = max(0, int(total_cents))
    lanes = list(lane_yields)
    if total == 0 or not lanes:
        return {}
    frac = min(1.0, max(0.0, exploration_frac))

    positives = {lane: max(0.0, float(y)) for lane, y in lane_yields.items()}
    pos_sum = sum(positives.values())

    if pos_sum <= 0:
        explore_pool, exploit_pool = total, 0
    else:
        explore_pool = int(round(total * frac))
        exploit_pool = total - explore_pool

    alloc = {lane: 0 for lane in lanes}
    if exploit_pool > 0 and pos_sum > 0:
        for lane in lanes:
            alloc[lane] += int(exploit_pool * positives[lane] / pos_sum)
    if explore_pool > 0:
        per = explore_pool // len(lanes)
        for lane in lanes:
            alloc[lane] += per

    # Hand any rounding remainder to the highest-yield lane so the budget is fully used.
    spent = sum(alloc.values())
    leftover = total - spent
    if leftover > 0:
        best = max(lanes, key=lambda l: positives.get(l, 0.0))
        alloc[best] += leftover
    return alloc


def plan_allocation(total_cents: int, *, exploration_frac: float = 0.2) -> dict[str, int]:
    """
    Allocate today's budget across the lanes the reward system has seen, using their
    realized yields from the process-global YieldTracker. Convenience wrapper over
    `allocate_budget`.
    """
    from sovereign_os.governance.portfolio import _YIELD

    snap = _YIELD.snapshot()
    lane_yields = {lane: vals.get("yield", 0.0) for lane, vals in snap.items()}
    return allocate_budget(total_cents, lane_yields, exploration_frac=exploration_frac)
