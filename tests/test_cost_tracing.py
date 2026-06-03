"""
Tests for cost tracing & control: per-model pricing, ledger cost rollups,
per-task spend ceiling, and the estimate→actual budget-overrun loop.
"""

import pytest

from sovereign_os.agents.auth import SovereignAuth
from sovereign_os.governance.engine import GovernanceEngine
from sovereign_os.governance.exceptions import FiscalInsolvencyError
from sovereign_os.governance.pricing import (
    estimate_cost_cents,
    estimate_cost_usd,
    get_model_pricing,
)
from sovereign_os.governance.treasury import Treasury
from sovereign_os.ledger.unified_ledger import UnifiedLedger
from sovereign_os.models.charter import Charter, FiscalBoundaries


# ------------------------------------------------------------------- pricing
def test_pricing_differs_by_model():
    # o1 is ~100x pricier than gpt-4o-mini for the same tokens.
    mini = estimate_cost_usd("gpt-4o-mini", 1_000_000, 1_000_000)
    o1 = estimate_cost_usd("o1", 1_000_000, 1_000_000)
    assert mini == pytest.approx(0.75)      # 0.15 + 0.60
    assert o1 == pytest.approx(75.0)        # 15 + 60
    assert o1 > mini * 50


def test_pricing_prefix_match_and_fallback():
    # Dated/suffixed ids resolve to the base model.
    assert get_model_pricing("gpt-4o-2024-11-20") == get_model_pricing("gpt-4o")
    assert get_model_pricing("claude-3-5-sonnet-20241022") == get_model_pricing("claude-3-5-sonnet")
    # gpt-4o-mini must NOT collapse into gpt-4o (longest-prefix wins).
    assert get_model_pricing("gpt-4o-mini") != get_model_pricing("gpt-4o")
    # Unknown model -> conservative fallback, not zero.
    assert get_model_pricing("totally-unknown-model")[0] > 0


def test_pricing_env_override(monkeypatch):
    monkeypatch.setenv("SOVEREIGN_MODEL_PRICING_JSON", '{"my-model": [1.0, 3.0]}')
    assert get_model_pricing("my-model") == (1.0, 3.0)
    assert estimate_cost_cents("my-model", 1_000_000, 1_000_000) == 400  # $4.00


def test_estimate_cost_cents_rounding():
    # Sub-cent rounds to 0; this is documented behavior.
    assert estimate_cost_cents("gpt-4o-mini", 100, 100) == 0
    # A realistic article: 4k in + 2k out on gpt-4o = 1 + 2 cents = 3 cents.
    assert estimate_cost_cents("gpt-4o", 4000, 2000) == 3


# --------------------------------------------------------------- ledger trace
def test_ledger_cost_rollups():
    led = UnifiedLedger()
    led.record_token("gpt-4o", 1000, 500, agent_id="research", task_id="t1", estimated_usd_cents=5)
    led.record_token("gpt-4o-mini", 2000, 1000, agent_id="writer", task_id="t2", estimated_usd_cents=1)
    led.record_token("gpt-4o", 500, 200, agent_id="research", task_id="t3", estimated_usd_cents=2)

    assert led.cost_cents_by_model() == {"gpt-4o": 7, "gpt-4o-mini": 1}
    assert led.cost_cents_by_agent() == {"research": 7, "writer": 1}
    assert led.cost_cents_by_task() == {"t1": 5, "t2": 1, "t3": 2}

    summary = led.cost_summary()
    assert summary["token_cost_cents"] == 8
    assert summary["total_input_tokens"] == 3500
    assert summary["total_output_tokens"] == 1700
    assert summary["total_tokens"] == 5200


# ------------------------------------------------------------ per-task ceiling
def test_max_task_cost_ceiling():
    led = UnifiedLedger()
    led.record_usd(100_000)  # plenty of balance
    charter = Charter(
        mission="m",
        fiscal_boundaries=FiscalBoundaries(max_task_cost_usd=1.00),  # $1 hard ceiling
    )
    t = Treasury(charter, led)
    t.approve_task(100, task_id="ok")  # exactly $1.00 — allowed
    with pytest.raises(FiscalInsolvencyError):
        t.approve_task(101, task_id="too-big")  # $1.01 — denied despite ample balance


# ------------------------------------------------- estimate -> actual overrun
@pytest.mark.asyncio
async def test_budget_overrun_loop(charter, ledger, auth):
    events: list[tuple[str, dict]] = []
    engine = GovernanceEngine(
        charter, ledger, auth=auth,
        on_event=lambda ev, data: events.append((ev, data)),
    )
    before = auth.get_trust_score("research-agent")
    engine._task_estimate_cents["task-1"] = 10  # CFO budgeted 10 cents

    engine._reconcile_cost("task-1", "research-agent", actual_cents=20)  # +100% over

    assert auth.get_trust_score("research-agent") < before  # trust docked
    assert any(ev == "budget_overrun" and data["task_id"] == "task-1" for ev, data in events)


@pytest.mark.asyncio
async def test_budget_overrun_within_tolerance_is_ignored(charter, ledger, auth):
    engine = GovernanceEngine(charter, ledger, auth=auth)
    before = auth.get_trust_score("agent-2")
    engine._task_estimate_cents["task-2"] = 100
    engine._reconcile_cost("task-2", "agent-2", actual_cents=110)  # +10% < 25% tolerance
    assert auth.get_trust_score("agent-2") == before  # no penalty


# ------------------------------------------------ mission budget exhaustion
@pytest.mark.asyncio
async def test_dispatch_halts_when_mission_budget_exhausted(auth):
    from sovereign_os.governance.strategist import PlannedTask, TaskPlan

    led = UnifiedLedger()
    led.record_usd(100_000)
    charter = Charter(
        mission="m",
        fiscal_boundaries=FiscalBoundaries(max_mission_cost_usd=0.10),  # 10-cent mission cap
    )
    events: list[tuple[str, dict]] = []
    engine = GovernanceEngine(
        charter, led, auth=auth,
        cost_converter=lambda t: 20,  # each task "costs" 20 cents -> cap blown after task 1
        on_event=lambda ev, data: events.append((ev, data)),
    )
    # task-2 depends on task-1, so it lands in a second wave that never launches.
    plan = TaskPlan(
        goal_summary="two-step",
        tasks=[
            PlannedTask(task_id="task-1", description="first", dependencies=[],
                        required_skill="research", estimated_token_budget=500, priority="low"),
            PlannedTask(task_id="task-2", description="second", dependencies=["task-1"],
                        required_skill="research", estimated_token_budget=500, priority="low"),
        ],
    )
    results = await engine.dispatch(plan)
    by_id = {r.task_id: r for r in results}
    assert by_id["task-1"].success is True                         # first task ran
    assert by_id["task-2"].metadata.get("error") == "budget_halt"  # second halted
    assert any(ev == "mission_budget_exhausted" for ev, _ in events)
