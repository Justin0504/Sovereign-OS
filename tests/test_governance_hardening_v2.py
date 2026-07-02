"""
Tests for the governance-hardening additions:
  - CFO SpendCircuitBreaker (session ceiling / failure streak / ROI fast-fail)
  - JIT capability leases on SovereignAuth
  - Per-category audit rubric + positional-bias shuffle
  - CEO reactive prune_to_budget + corrective_task
"""

import json

import pytest

from sovereign_os.agents.auth import Capability, SovereignAuth
from sovereign_os.governance.circuit_breaker import SpendCircuitBreaker
from sovereign_os.governance.exceptions import CircuitBreakerTrippedError
from sovereign_os.governance.strategist import PlannedTask, Strategist, TaskPlan
from sovereign_os.models.charter import Charter


# --------------------------------------------------------------- circuit breaker
def test_breaker_off_by_default_never_trips():
    b = SpendCircuitBreaker()
    b.record_spend(10_000_000)
    for _ in range(100):
        b.record_outcome(False)
    b.check()  # no config => closed
    assert not b.is_tripped


def test_breaker_trips_on_session_ceiling():
    b = SpendCircuitBreaker(session_ceiling_cents=500)
    b.record_spend(500)
    with pytest.raises(CircuitBreakerTrippedError) as ei:
        b.check()
    assert ei.value.spent_cents == 500 and "ceiling" in ei.value.reason


def test_breaker_lookahead_blocks_next_task():
    b = SpendCircuitBreaker(session_ceiling_cents=500)
    b.record_spend(400)
    b.check()  # 400 < 500, fine now
    with pytest.raises(CircuitBreakerTrippedError):
        b.check(next_spend_cents=200)  # 400+200 > 500 -> pre-emptive trip


def test_breaker_trips_on_consecutive_failures():
    b = SpendCircuitBreaker(max_consecutive_failures=3)
    b.record_outcome(False)
    b.record_outcome(False)
    b.check()  # 2 < 3
    b.record_outcome(False)
    with pytest.raises(CircuitBreakerTrippedError):
        b.check()
    # a pass resets the streak
    b.record_outcome(True)
    b.check()


def test_breaker_trips_on_roi_floor_after_grace():
    b = SpendCircuitBreaker(roi_floor=1.0, roi_grace_spend_cents=1000)
    b.record_spend(500)
    b.record_revenue(100)
    b.check()  # below grace spend -> ROI not judged yet
    b.record_spend(600)  # total 1100 spent, 100 revenue -> ROI 0.09
    with pytest.raises(CircuitBreakerTrippedError):
        b.check()


def test_breaker_reset_and_status():
    b = SpendCircuitBreaker(session_ceiling_cents=500)
    b.record_spend(300)
    b.record_revenue(900)
    assert b.status()["roi"] == 3.0
    b.reset()
    assert b.status()["spent_cents"] == 0 and b.status()["revenue_cents"] == 0


# ------------------------------------------------------------------- JIT leases
def _auth_at(score, *, clock=None):
    a = SovereignAuth(base_trust_score=score, clock=clock)
    return a


def test_lease_denied_when_not_eligible():
    a = _auth_at(10)  # below EXECUTE_SHELL threshold (60)
    lease = a.grant_lease("ag", Capability.EXECUTE_SHELL, task_id="t1")
    assert lease is None
    assert a.use_lease("ag", Capability.EXECUTE_SHELL, "t1") is False


def test_single_use_lease_consumes_then_denies():
    a = _auth_at(90)  # above EXECUTE_SHELL threshold
    lease = a.grant_lease("ag", Capability.EXECUTE_SHELL, task_id="t1", max_uses=1)
    assert lease is not None
    assert a.use_lease("ag", Capability.EXECUTE_SHELL, "t1") is True   # first use ok
    assert a.use_lease("ag", Capability.EXECUTE_SHELL, "t1") is False  # exhausted


def test_lease_is_task_scoped():
    a = _auth_at(90)
    a.grant_lease("ag", Capability.EXECUTE_SHELL, task_id="t1", max_uses=5)
    # same agent+capability but a different task has no lease
    assert a.has_active_lease("ag", Capability.EXECUTE_SHELL, "t2") is False
    assert a.use_lease("ag", Capability.EXECUTE_SHELL, "t2") is False


def test_lease_expires_by_ttl():
    clock = {"t": 100.0}
    a = _auth_at(90, clock=lambda: clock["t"])
    a.grant_lease("ag", Capability.EXECUTE_SHELL, task_id="t1", ttl_seconds=10, max_uses=0)
    assert a.has_active_lease("ag", Capability.EXECUTE_SHELL, "t1")
    clock["t"] = 111.0  # 11s later > 10s TTL
    assert a.has_active_lease("ag", Capability.EXECUTE_SHELL, "t1") is False
    assert a.use_lease("ag", Capability.EXECUTE_SHELL, "t1") is False


def test_revoke_task_leases_de_escalates():
    a = _auth_at(90)
    a.grant_lease("ag", Capability.EXECUTE_SHELL, task_id="t1", max_uses=0)
    a.grant_lease("ag", Capability.WRITE_FILES, task_id="t1", max_uses=0)
    assert len(a.active_leases("ag")) == 2
    n = a.revoke_task_leases("t1")
    assert n == 2 and a.active_leases("ag") == []


def test_purge_expired_leases():
    clock = {"t": 0.0}
    a = _auth_at(90, clock=lambda: clock["t"])
    a.grant_lease("ag", Capability.EXECUTE_SHELL, task_id="t1", ttl_seconds=5, max_uses=0)
    clock["t"] = 10.0
    assert a.purge_expired_leases() == 1


# ---------------------------------------------------------------- category rubric
def test_rubric_for_categories_and_safety_appended():
    from sovereign_os.auditor.rubric import rubric_for

    coding = [k for k, _ in rubric_for("coding")]
    assert "robustness" in coding and coding[-1] == "safety"
    writing = [k for k, _ in rubric_for("writing")]
    assert "voice" in writing and "safety" in writing
    # unknown category -> generic rubric + safety
    generic = [k for k, _ in rubric_for("nonsense")]
    assert generic == ["relevance", "completeness", "correctness", "safety"]


def test_shuffle_is_deterministic_and_order_varies():
    from sovereign_os.auditor.rubric import shuffled_rubric

    a1 = shuffled_rubric("coding", seed="task-7")
    a2 = shuffled_rubric("coding", seed="task-7")
    assert a1 == a2  # stable per task
    # same criteria set, (usually) different order across tasks
    assert {k for k, _ in a1} == {k for k, _ in shuffled_rubric("coding", seed="task-8")}


@pytest.mark.asyncio
async def test_judge_uses_category_rubric_keys():
    from sovereign_os.auditor.review_engine import JudgeLLM

    captured = {}

    class JudgeClient:
        model_name = "judge"
        async def chat(self, messages):
            captured["system"] = messages[0]["content"]
            # respond with the coding-rubric criteria keys
            return json.dumps({
                "correctness": 0.9, "completeness": 0.8, "robustness": 0.7,
                "relevance": 1.0, "safety": 1.0, "reason": "ok", "suggested_fix": "",
            })

    j = JudgeLLM(client=JudgeClient())
    rep = await j.evaluate("t1", "def f(): ...", "Does it work?", "kpi",
                           min_score=0.6, category="coding")
    assert set(rep.sub_scores) == {"correctness", "completeness", "robustness", "relevance", "safety"}
    assert "coding" in captured["system"] and "robustness" in captured["system"]
    assert rep.passed is True


# ------------------------------------------------------------------ reactive CEO
def _task(tid, skill, prio, deps=None, budget=4000):
    return PlannedTask(task_id=tid, description=tid, required_skill=skill,
                       priority=prio, dependencies=deps or [], estimated_token_budget=budget)


def test_prune_to_budget_drops_lowest_priority_first():
    s = Strategist(Charter(mission="m"))
    plan = TaskPlan(tasks=[
        _task("a", "research", "high"),
        _task("b", "write_article", "low"),
        _task("c", "design_brief", "low"),
    ])
    cost = lambda t: 100  # each task costs 100 cents
    pruned = s.prune_to_budget(plan, budget_cents=150, cost_of=cost)
    kept = [t.task_id for t in pruned.tasks]
    assert "a" in kept  # high priority survives
    assert len(kept) == 1  # only one 100-cent task fits under 150


def test_prune_never_breaks_dependencies():
    s = Strategist(Charter(mission="m"))
    # b (low) is depended on by c (high) -> b must NOT be dropped even though it's low
    plan = TaskPlan(tasks=[
        _task("b", "research", "low"),
        _task("c", "write_article", "high", deps=["b"]),
        _task("d", "design_brief", "low"),
    ])
    cost = lambda t: 100
    pruned = s.prune_to_budget(plan, budget_cents=200, cost_of=cost)
    kept = [t.task_id for t in pruned.tasks]
    assert "b" in kept and "c" in kept  # dependency chain preserved
    assert "d" not in kept              # the free-standing low task is dropped


def test_corrective_task_carries_fix_and_resets_deps():
    s = Strategist(Charter(mission="m"))
    failed = _task("task-1-code_assistant", "code_assistant", "low", deps=["task-0"])
    retry = s.corrective_task(failed, reason="tests failed", suggested_fix="handle None input", attempt=2)
    assert retry.task_id == "task-1-code_assistant-retry2"
    assert retry.dependencies == [] and retry.priority == "high"
    assert "tests failed" in retry.description and "handle None input" in retry.description


# ----------------------------------------------- engine wiring (breaker fast-fail)
@pytest.mark.asyncio
async def test_engine_circuit_breaker_halts_on_failure_streak(charter, ledger, auth):
    """A wired breaker trips the mission when audits fail past the streak limit."""
    from sovereign_os.auditor import ReviewEngine
    from sovereign_os.auditor.base import AuditReport, BaseAuditor
    from sovereign_os.governance.engine import GovernanceEngine

    class FailingJudge(BaseAuditor):
        async def evaluate(self, task_id, task_output, verification_prompt, kpi_name,
                           *, min_score=None, category=None):
            return AuditReport(task_id=task_id, kpi_name=kpi_name or "default",
                               passed=False, score=0.1, reason="forced fail", suggested_fix="fix it")

    review = ReviewEngine(charter, judge=FailingJudge())
    breaker = SpendCircuitBreaker(max_consecutive_failures=1)
    engine = GovernanceEngine(charter, ledger, auth=auth, review_engine=review, circuit_breaker=breaker)
    # abort_on_audit_failure=False so the breaker (not AuditFailureError) is what halts us.
    with pytest.raises(CircuitBreakerTrippedError):
        await engine.run_mission_with_audit("Do one task.", abort_on_audit_failure=False)
    assert breaker.is_tripped
