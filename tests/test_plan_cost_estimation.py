"""
Tests for second-stage (plan-derived) cost estimation.

The measured failure of `complexity_from_goal` is a slope error: across a suite whose
real cost spanned 12.8x, the goal-string score moved 1.21x. The point of these tests is
that a plan-derived signal actually responds to the size of the work — which is the
property the goal-string one lacks.
"""

import pytest

from sovereign_os.governance.cost_model import record_cost, reset_cost
from sovereign_os.governance.economics import (
    complexity_from_goal,
    estimate_plan_cost_cents,
    plan_complexity,
)
from sovereign_os.governance.strategist import PlannedTask, TaskPlan


@pytest.fixture(autouse=True)
def clean_global():
    reset_cost()
    yield
    reset_cost()


def _plan(n, *, skill="coding", budget=8000, chain=False):
    tasks = []
    for i in range(n):
        deps = [f"t{i - 1}"] if (chain and i) else []
        tasks.append(PlannedTask(
            task_id=f"t{i}", description=f"step {i}", dependencies=deps,
            required_skill=skill, estimated_token_budget=budget,
        ))
    return TaskPlan(goal_summary="g", tasks=tasks)


# ------------------------------------------------------------------- complexity

def test_plan_complexity_responds_to_task_count():
    """
    The property the goal-string heuristic lacks: it moves with the work.

    The scalar is deliberately sub-linear in task count (^0.75), so it spans ~2.5x from
    one task to eight rather than 8x — it is a difficulty *modifier*, not the estimate.
    Linear scaling lives in `estimate_plan_cost_cents`, pinned separately below. What
    matters here is that it comfortably clears the 1.21x the goal-string score managed
    across work whose real cost spanned 12.8x.
    """
    one, three, eight = plan_complexity(_plan(1)), plan_complexity(_plan(3)), plan_complexity(_plan(8))
    assert one < three < eight
    assert eight / one > 2.0, "must span well beyond the goal-string score's 1.21x"


def test_plan_complexity_beats_goal_string_on_dynamic_range():
    """
    Same comparison the calibration run made, in miniature: a one-task plan against an
    eight-task plan, versus a short goal against a long one.
    """
    goal_span = complexity_from_goal("Write a function.") and (
        complexity_from_goal("Refactor the client into an async one with retries, pooling, "
                             "timeouts and a full test suite covering each failure mode.")
        / complexity_from_goal("Write a function.")
    )
    plan_span = plan_complexity(_plan(8)) / plan_complexity(_plan(1))
    assert plan_span > goal_span


def test_dependency_depth_raises_complexity():
    """Serial work re-reads context, so a chain costs more than the same tasks in parallel."""
    assert plan_complexity(_plan(4, chain=True)) > plan_complexity(_plan(4, chain=False))


def test_empty_plan_falls_back_to_neutral():
    assert plan_complexity(TaskPlan(goal_summary="g", tasks=[])) == 1.0
    assert plan_complexity(None) == 1.0


def test_cyclic_plan_does_not_hang():
    """A malformed plan must not send the depth walk into infinite recursion."""
    plan = TaskPlan(goal_summary="g", tasks=[
        PlannedTask(task_id="a", dependencies=["b"], required_skill="coding"),
        PlannedTask(task_id="b", dependencies=["a"], required_skill="coding"),
    ])
    assert plan_complexity(plan) > 0


def test_complexity_is_bounded():
    assert plan_complexity(_plan(200)) <= 4.0


# ------------------------------------------------------------------------- cost

def test_cost_scales_with_the_number_of_tasks():
    one, _ = estimate_plan_cost_cents(_plan(1), calibrated=False)
    four, _ = estimate_plan_cost_cents(_plan(4), calibrated=False)
    assert four > one
    assert four == pytest.approx(4 * one, rel=0.1)


def test_breakdown_reports_each_task_and_its_source():
    total, breakdown = estimate_plan_cost_cents(_plan(3), calibrated=False)
    assert len(breakdown) == 3
    assert sum(b["cents"] for b in breakdown) == total
    assert {b["source"] for b in breakdown} == {"planner_budget"}
    assert [b["task_id"] for b in breakdown] == ["t0", "t1", "t2"]


def test_unbudgeted_task_falls_back_to_the_category_prior():
    """The planner sometimes omits a budget; that task still has to be costed."""
    plan = TaskPlan(goal_summary="g", tasks=[
        PlannedTask(task_id="t0", required_skill="coding", estimated_token_budget=0),
    ])
    total, breakdown = estimate_plan_cost_cents(plan, calibrated=False)
    assert total > 0
    assert breakdown[0]["source"] == "category_prior"


def test_unknown_skill_is_costed_as_general():
    plan = TaskPlan(goal_summary="g", tasks=[
        PlannedTask(task_id="t0", required_skill="interpretive_dance",
                    estimated_token_budget=5000),
    ])
    _, breakdown = estimate_plan_cost_cents(plan, calibrated=False)
    assert breakdown[0]["category"] == "general"


def test_empty_plan_costs_nothing():
    total, breakdown = estimate_plan_cost_cents(TaskPlan(goal_summary="g", tasks=[]))
    assert total == 0 and breakdown == []


def test_calibration_factor_is_applied_when_requested():
    """The learned per-category correction rides on the plan estimate too."""
    raw, _ = estimate_plan_cost_cents(_plan(2), calibrated=False)
    for _ in range(40):
        record_cost("coding", 10, 40)          # teach a large upward correction
    calibrated, _ = estimate_plan_cost_cents(_plan(2), calibrated=True)
    assert calibrated > raw


def test_uncalibrated_path_ignores_learned_history():
    raw_before, _ = estimate_plan_cost_cents(_plan(2), calibrated=False)
    for _ in range(40):
        record_cost("coding", 10, 40)
    raw_after, _ = estimate_plan_cost_cents(_plan(2), calibrated=False)
    assert raw_after == raw_before
