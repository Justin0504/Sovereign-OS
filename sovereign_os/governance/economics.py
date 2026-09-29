"""
Opportunity economics: decide WHICH inbound tasks are worth taking.

The CFO's `approve_job_profitability` is a *gate at mission start* — by the time it
fires, the goal is already planned. On real agent-bounty platforms that is too late
and too coarse: agents lose money not on the model tokens but on settlement/gas fees
and on taking marginal work at all (a widely-cited 2026 P&L showed an agent net
**-$8.30** over four days, the losses dominated by gas and bridging, even though the
work shipped). Profitable autonomy therefore needs a *screen before compute*: given a
candidate task's payout, estimate the fully-loaded cost (LLM + settlement fee + gas)
and skip anything that can't clear the margin floor.

This module is pure and deterministic so it can run in the ingest hot path and be
unit-tested without a network or an LLM.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from sovereign_os.governance.pricing import estimate_budget_cost_cents

# Rough token budget a task of a given category needs end-to-end (plan + work +
# audit). Deliberately conservative — better to over-estimate cost and skip a
# marginal task than to take a loser. Overridable per deployment if needed.
_CATEGORY_TOKENS: dict[str, int] = {
    "coding": 24000,     # read repo + write + tests + PR + audit
    "data": 16000,
    "research": 14000,
    "design": 12000,
    "writing": 10000,
    "automation": 12000,
    "email": 4000,
    "general": 8000,
}
_DEFAULT_TOKENS = 10000

# Output-heavy categories cost more (output tokens are pricier).
_CATEGORY_OUTPUT_RATIO: dict[str, float] = {
    "coding": 0.6, "writing": 0.75, "design": 0.6, "email": 0.6,
    "data": 0.4, "research": 0.4, "automation": 0.5, "general": 0.5,
}


@dataclass
class Opportunity:
    """Verdict for one candidate task."""

    take: bool
    revenue_cents: int
    est_cost_cents: int      # LLM cost
    fee_cents: int           # settlement fee (percentage of payout)
    gas_cents: int           # fixed on-chain/network cost per task
    net_margin_cents: int    # payout - fee - gas - LLM cost
    margin_ratio: float      # net_margin / revenue (0 when revenue is 0)
    reason: str

    def as_dict(self) -> dict:
        return {
            "take": self.take, "revenue_cents": self.revenue_cents,
            "est_cost_cents": self.est_cost_cents, "fee_cents": self.fee_cents,
            "gas_cents": self.gas_cents, "net_margin_cents": self.net_margin_cents,
            "margin_ratio": round(self.margin_ratio, 4), "reason": self.reason,
        }


def estimate_task_cost_cents(
    category: str, model: str = "gpt-4o", *, complexity: float = 1.0, calibrated: bool = True
) -> int:
    """
    Pre-flight LLM cost (cents) for a task of `category`, scaled by `complexity`
    (1.0 = typical; a long/hard goal can pass e.g. 1.5). Uses the same real per-model
    pricing the ledger records actuals against.

    When `calibrated` (default), the heuristic is multiplied by the per-category
    correction learned from settled jobs' real costs (`cost_model.cost_factor`), so
    estimates converge on reality; pass `calibrated=False` to get the raw heuristic
    (used when recording estimate-vs-actual, to keep the learned factor unbiased).
    """
    base_tokens = _CATEGORY_TOKENS.get((category or "").lower(), _DEFAULT_TOKENS)
    tokens = int(base_tokens * max(0.1, complexity))
    ratio = _CATEGORY_OUTPUT_RATIO.get((category or "").lower(), 0.5)
    raw = estimate_budget_cost_cents(model, tokens, output_ratio=ratio)
    if not calibrated:
        return raw
    try:
        from sovereign_os.governance.cost_model import cost_factor

        return max(1, int(round(raw * cost_factor(category))))
    except Exception:  # noqa: BLE001 - calibration is best-effort
        return raw


def complexity_from_goal(goal: str) -> float:
    """
    Cheap heuristic: longer, multi-part goals cost more. Range ~0.7–2.0.

    Measured limitation (17-task suite, Haiku 4.5, 2026-09-17): across goals whose real
    cost spanned 12.8x, this score moved only 1.21x, and the resulting estimate only
    1.14x. It is a function of goal *string length*, and a short sentence can describe an
    enormous amount of work. The consequence is a systematic slope error — small tasks
    over-estimated ~1.8x, large ones under-estimated ~1.7x — which no per-category
    multiplier can remove, because a single factor shifts the line without changing its
    slope. Use `plan_complexity`/`estimate_plan_cost_cents` once a plan exists.
    """
    n = len(goal or "")
    parts = 1 + (goal or "").count("\n") + (goal or "").lower().count(" and ")
    length_factor = min(1.6, 0.7 + n / 1200.0)
    return round(min(2.0, length_factor * min(1.5, 0.85 + 0.15 * parts)), 3)


def plan_complexity(plan) -> float:
    """
    Difficulty implied by an actual task plan, rather than by the goal's prose.

    A plan is available after decomposition and before execution — the moment the budget
    ceiling is set — and carries the two things goal text cannot: how many units of work
    the problem broke into, and how deep the dependency chain runs. Scales roughly with
    task count, tempered so a ten-task plan is not asserted to be ten times a one-task
    plan, and nudged by chain depth because serial work re-reads context.

    Returns ~0.7–4.0. An empty plan falls back to 1.0 rather than claiming zero work.
    """
    tasks = list(getattr(plan, "tasks", None) or [])
    if not tasks:
        return 1.0

    count_factor = 0.7 + 0.45 * (len(tasks) ** 0.75)

    by_id = {getattr(t, "task_id", ""): t for t in tasks}
    depth_cache: dict[str, int] = {}

    def depth(task_id: str, seen: frozenset = frozenset()) -> int:
        if task_id in depth_cache:
            return depth_cache[task_id]
        if task_id in seen:          # a cyclic plan is malformed; do not recurse forever
            return 0
        task = by_id.get(task_id)
        deps = [d for d in (getattr(task, "dependencies", None) or []) if d in by_id]
        value = 1 + max((depth(d, seen | {task_id}) for d in deps), default=0)
        depth_cache[task_id] = value
        return value

    chain = max((depth(tid) for tid in by_id), default=1)
    depth_factor = 1.0 + 0.08 * max(0, chain - 1)
    return round(min(4.0, count_factor * depth_factor), 3)


def estimate_plan_cost_cents(
    plan,
    model: str = "gpt-4o",
    *,
    calibrated: bool = True,
    default_category: str = "general",
) -> tuple[int, list[dict]]:
    """
    Second-stage estimate: price an actual plan, task by task.

    The first-stage estimate (`screen_task`) has to run before any compute is spent, so
    all it can see is the goal string — and measurement shows that is a weak predictor.
    Once the planner has decomposed the goal, far more is known: how many tasks there
    are, what competency each needs, and the planner's own token budget for each. This
    is still available *before* execution, which is where a budget ceiling belongs.

    Returns `(total_cents, per_task_breakdown)`.

    A caveat worth keeping in view: `estimated_token_budget` is itself produced by an
    LLM, so it inherits the estimation error under study. The claim here is only that it
    is produced *after* the model has actually decomposed the problem, and therefore
    rests on strictly more information than the goal string does. Whether that converts
    into a better estimate is an empirical question — `bench.calibration_run` records
    both stages against the same realized cost so the two can be compared directly.
    """
    tasks = list(getattr(plan, "tasks", None) or [])
    breakdown: list[dict] = []
    total = 0

    for task in tasks:
        skill = (getattr(task, "required_skill", "") or "").lower()
        category = skill if skill in _CATEGORY_TOKENS else default_category
        budget = int(getattr(task, "estimated_token_budget", 0) or 0)

        if budget > 0:
            ratio = _CATEGORY_OUTPUT_RATIO.get(category, 0.5)
            cents = estimate_budget_cost_cents(model, budget, output_ratio=ratio)
            if calibrated:
                try:
                    from sovereign_os.governance.cost_model import cost_factor

                    cents = max(1, int(round(cents * cost_factor(category))))
                except Exception:  # noqa: BLE001 - calibration is best-effort
                    pass
            source = "planner_budget"
        else:
            # The planner declined to budget this task; fall back to the category prior.
            cents = estimate_task_cost_cents(category, model, calibrated=calibrated)
            source = "category_prior"

        total += cents
        breakdown.append({
            "task_id": getattr(task, "task_id", ""),
            "category": category,
            "tokens": budget,
            "cents": cents,
            "source": source,
        })

    return total, breakdown


def evaluate_opportunity(
    revenue_cents: int,
    est_cost_cents: int,
    *,
    fee_ratio: float = 0.0,
    gas_cents: int = 0,
    margin_floor: float = 0.0,
) -> Opportunity:
    """
    Decide whether a task clears the bar. Take it iff the fully-loaded net margin
    (payout - settlement fee - gas - LLM cost) is >= margin_floor share of the payout
    (and strictly positive). Free/unpriced tasks (revenue<=0) are taken only when the
    floor is 0, since there's no money to lose — useful for reputation-building work.
    """
    revenue_cents = max(0, int(revenue_cents))
    fee_ratio = min(1.0, max(0.0, fee_ratio))
    fee_cents = int(round(revenue_cents * fee_ratio))
    gas_cents = max(0, int(gas_cents))
    est_cost_cents = max(0, int(est_cost_cents))
    net_margin = revenue_cents - fee_cents - gas_cents - est_cost_cents
    margin_ratio = (net_margin / revenue_cents) if revenue_cents > 0 else 0.0

    if revenue_cents <= 0:
        take = margin_floor <= 0
        reason = ("unpaid task accepted (no floor; reputation work)" if take
                  else "unpaid task skipped (margin floor set)")
        return Opportunity(take, revenue_cents, est_cost_cents, fee_cents, gas_cents,
                           net_margin, margin_ratio, reason)

    required = revenue_cents * margin_floor
    if net_margin <= 0:
        take, reason = False, (
            f"unprofitable: net margin {net_margin}¢ ≤ 0 "
            f"(payout {revenue_cents}¢ − fee {fee_cents}¢ − gas {gas_cents}¢ − LLM {est_cost_cents}¢)"
        )
    elif net_margin < required:
        take, reason = False, (
            f"below margin floor: net {net_margin}¢ ({margin_ratio*100:.0f}%) "
            f"< required {required:.0f}¢ ({margin_floor*100:.0f}%)"
        )
    else:
        take, reason = True, (
            f"profitable: net {net_margin}¢ ({margin_ratio*100:.0f}%) "
            f"after fee {fee_cents}¢ + gas {gas_cents}¢ + LLM {est_cost_cents}¢"
        )
    return Opportunity(take, revenue_cents, est_cost_cents, fee_cents, gas_cents,
                       net_margin, margin_ratio, reason)


def screen_task(
    revenue_cents: int,
    goal: str,
    category: str,
    *,
    model: str = "gpt-4o",
    fee_ratio: float | None = None,
    gas_cents: int | None = None,
    margin_floor: float | None = None,
) -> Opportunity:
    """
    One-call screen for the ingest hot path: estimate cost from the goal/category,
    then evaluate against fee/gas/floor. Fee/gas/floor default from env so an
    operator tunes the whole fleet without touching code:

      SOVEREIGN_SETTLEMENT_FEE_RATIO  (e.g. 0.029 for a 2.9% rail)
      SOVEREIGN_GAS_COST_CENTS        (fixed per-task on-chain cost, e.g. 5)
      SOVEREIGN_MIN_MARGIN_RATIO      (require this net margin, e.g. 0.3)
    """
    fee_ratio = _env_float("SOVEREIGN_SETTLEMENT_FEE_RATIO", 0.0) if fee_ratio is None else fee_ratio
    gas_cents = _env_int("SOVEREIGN_GAS_COST_CENTS", 0) if gas_cents is None else gas_cents
    margin_floor = _env_float("SOVEREIGN_MIN_MARGIN_RATIO", 0.0) if margin_floor is None else margin_floor
    est = estimate_task_cost_cents(category, model, complexity=complexity_from_goal(goal))
    return evaluate_opportunity(
        revenue_cents, est, fee_ratio=fee_ratio, gas_cents=gas_cents, margin_floor=margin_floor,
    )


def _env_float(name: str, default: float) -> float:
    try:
        return float((os.getenv(name) or "").strip() or default)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int((os.getenv(name) or "").strip() or default)
    except ValueError:
        return default
