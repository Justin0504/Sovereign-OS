"""
BE-03, retry amplification: a self-repair loop that re-runs a failing task N times burns
N times the tokens, and the session circuit breaker must see that spend.

The threat model's stated defense was: "Repair attempts draw from the same task budget,
and the circuit breaker fast-fails a session whose retry spend crosses a threshold." The
code did neither reliably. The breaker was fed the task's static *estimate*, recorded once,
so N repairs moved the session counter by one estimate. And _repair_task re-enters
dispatch(), which reset the cumulative-spend counter to zero, so the mission cap did not
see repair spend accumulate either.

The breaker is now fed the delta of actual (metered) mission spend, and only the outermost
dispatch resets the counter. These tests drive the real run_mission_with_audit repair loop
with a worker that always fails audit and burns real tokens each attempt.
"""

import asyncio

import pytest

from sovereign_os.agents.base import BaseWorker, TaskResult
from sovereign_os.agents.registry import WorkerRegistry
from sovereign_os.auditor.base import AuditReport
from sovereign_os.governance.circuit_breaker import SpendCircuitBreaker
from sovereign_os.governance.engine import GovernanceEngine
from sovereign_os.governance.exceptions import CircuitBreakerTrippedError
from sovereign_os.governance.strategist import PlannedTask, TaskPlan
from sovereign_os.ledger.unified_ledger import UnifiedLedger
from sovereign_os.llm import usage_meter
from sovereign_os.models.charter import Charter, FiscalBoundaries

BURN_PER_ATTEMPT = ("claude-opus-5", 5000, 5000)


class _BurnWorker(BaseWorker):
    """Burns a real, metered chunk of tokens every time it runs."""

    skill = "research"

    async def execute(self, task):
        usage_meter.record(*BURN_PER_ATTEMPT)
        return TaskResult(task_id=task.task_id, success=True, output="x",
                          metadata={"input_tokens": 5000, "output_tokens": 5000,
                                    "model_id": "claude-opus-5"})


class _AlwaysFailAudit:
    judge_model = "stub"

    async def audit_task(self, task, result, **k):
        return AuditReport(task_id=task.task_id, kpi_name="k", passed=False, score=0.1,
                           reason="forced failure", suggested_fix="try again")


def _engine(session_ceiling_cents):
    charter = Charter(mission="m", fiscal_boundaries=FiscalBoundaries(max_budget_usd=10_000.0))
    ledger = UnifiedLedger()
    ledger.record_usd(10_000_000, purpose="seed")
    reg = WorkerRegistry(charter)
    reg.register("research", _BurnWorker)
    reg.register("assistant_chat", _BurnWorker)
    reg.set_default(_BurnWorker)
    breaker = SpendCircuitBreaker(session_ceiling_cents=session_ceiling_cents)
    engine = GovernanceEngine(charter, ledger, registry=reg, circuit_breaker=breaker)
    engine._review_engine = _AlwaysFailAudit()
    # Build the corrective task without a model (offline), preserving the skill so the
    # default burn worker handles it.
    engine._strategist.corrective_task = lambda task, reason="", suggested_fix="", attempt=1: (
        PlannedTask(task_id=f"{task.task_id}-fix{attempt}", description=task.description,
                    dependencies=[], required_skill=task.required_skill,
                    estimated_token_budget=task.estimated_token_budget, priority="high"))
    return engine, ledger, breaker


def _run(engine, attempts):
    return asyncio.run(engine.run_mission_with_audit("do research", abort_on_audit_failure=False, max_repair_attempts=attempts))


def test_repair_spend_reaches_the_breaker():
    """
    The core of BE-03. The breaker's session counter must reflect the real tokens burned
    across every repair attempt, not one static estimate.
    """
    engine, ledger, breaker = _engine(session_ceiling_cents=10_000_000)
    _run(engine, attempts=5)
    real = ledger.total_token_estimated_usd_cents()
    assert real > 0
    assert breaker.spent_cents == pytest.approx(real, rel=0.01), (
        f"breaker saw {breaker.spent_cents}c but {real}c of tokens were actually burned")


def test_the_breaker_trips_when_retry_spend_crosses_the_ceiling():
    """
    One attempt's burn is under the ceiling; several are over it. The session must halt
    partway through the repair loop rather than running all attempts.
    """
    one_attempt = __import__("sovereign_os.governance.pricing", fromlist=["estimate_cost_cents"]).estimate_cost_cents(*BURN_PER_ATTEMPT)
    engine, ledger, breaker = _engine(session_ceiling_cents=int(one_attempt * 2.5))
    with pytest.raises(CircuitBreakerTrippedError):
        _run(engine, attempts=20)
    # It stopped early: far fewer than 20 attempts' worth of tokens were burned.
    assert ledger.total_token_estimated_usd_cents() < one_attempt * 6


def test_without_repair_the_breaker_still_sees_the_single_run():
    """The delta feed must not under-count the ordinary, no-repair case."""
    engine, ledger, breaker = _engine(session_ceiling_cents=10_000_000)
    _run(engine, attempts=0)
    assert breaker.spent_cents == pytest.approx(ledger.total_token_estimated_usd_cents(), rel=0.01)


def test_a_nested_repair_dispatch_does_not_reset_cumulative_spend():
    """
    _repair_task re-enters dispatch(). Only the outermost call resets the counter, or the
    original tasks' spend is discarded before the breaker is fed.
    """
    engine, ledger, breaker = _engine(session_ceiling_cents=10_000_000)
    _run(engine, attempts=3)
    # 1 original + 3 repairs = 4 attempts, each burning the same chunk.
    one = __import__("sovereign_os.governance.pricing", fromlist=["estimate_cost_cents"]).estimate_cost_cents(*BURN_PER_ATTEMPT)
    assert ledger.total_token_estimated_usd_cents() == pytest.approx(4 * one, rel=0.01)
    assert breaker.spent_cents == pytest.approx(4 * one, rel=0.01)
