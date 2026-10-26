"""
BE-04: cost is priced from the model the client layer actually invoked, not the one the
worker reports.

The ledger used to read input/output tokens and the model id from `result.metadata` --
the worker's own account of its spend. A worker that declares a cheap model while calling
an expensive one would be billed for the cheap one. The threat model's stated defense was
"recorded at the client layer below the agent's control"; the code recorded the worker's
report. It is also an invariant stated over a report, which R1 forbids.

A ContextVar accumulator (`usage_meter`) is opened by the engine around `worker.execute`
and written only by the LLM clients after a real response. A worker cannot write it
without making the calls. These tests drive the real dispatch path with a worker that
lies, and check the ledger priced the truth.
"""

import asyncio

import pytest

from sovereign_os.agents.base import BaseWorker, TaskResult
from sovereign_os.governance.engine import GovernanceEngine
from sovereign_os.governance.pricing import estimate_cost_cents
from sovereign_os.governance.strategist import PlannedTask, TaskPlan
from sovereign_os.agents.registry import WorkerRegistry
from sovereign_os.ledger.unified_ledger import UnifiedLedger
from sovereign_os.llm import usage_meter
from sovereign_os.models.charter import Charter, FiscalBoundaries

CHEAP = "claude-haiku-4-5"
EXPENSIVE = "claude-opus-4"


class _LyingWorker(BaseWorker):
    """Calls an expensive model but reports a cheap one and a tiny token count."""

    skill = "research"

    async def execute(self, task):
        # The real call, through the metered client layer.
        usage_meter.record(EXPENSIVE, 4000, 4000)
        # The worker's self-report: cheap model, few tokens.
        return TaskResult(
            task_id=task.task_id, success=True, output="done",
            metadata={"model_id": CHEAP, "input_tokens": 10, "output_tokens": 10},
        )


class _HonestWorker(BaseWorker):
    skill = "research"

    async def execute(self, task):
        usage_meter.record(CHEAP, 1000, 500)
        return TaskResult(
            task_id=task.task_id, success=True, output="done",
            metadata={"model_id": CHEAP, "input_tokens": 1000, "output_tokens": 500},
        )


class _SilentWorker(BaseWorker):
    """Makes no metered call -- e.g. a stub. Falls back to its own report."""

    skill = "research"

    async def execute(self, task):
        return TaskResult(
            task_id=task.task_id, success=True, output="done",
            metadata={"model_id": CHEAP, "input_tokens": 800, "output_tokens": 200},
        )


def _engine(worker_cls):
    charter = Charter(mission="m", fiscal_boundaries=FiscalBoundaries(max_budget_usd=100.0))
    ledger = UnifiedLedger()
    ledger.record_usd(100_000, purpose="seed")
    registry = WorkerRegistry(charter)
    registry.register(worker_cls.skill, worker_cls)
    engine = GovernanceEngine(charter, ledger, registry=registry)
    return engine, ledger


def _run(engine):
    plan = TaskPlan(goal_summary="g", tasks=[
        PlannedTask(task_id="task-1", description="do", dependencies=[],
                    required_skill="research", estimated_token_budget=4000, priority="high")])
    return asyncio.run(engine.dispatch(plan))


def _ledger_cents(ledger):
    return ledger.total_token_estimated_usd_cents()


def test_a_worker_is_billed_for_the_model_it_actually_called():
    engine, ledger = _engine(_LyingWorker)
    _run(engine)
    expensive = estimate_cost_cents(EXPENSIVE, 4000, 4000)
    cheap = estimate_cost_cents(CHEAP, 10, 10)
    billed = _ledger_cents(ledger)
    assert billed == expensive, f"billed {billed}, expected the expensive {expensive}"
    assert billed > cheap * 10, "the worker's cheap self-report was billed instead"


def test_the_metered_figure_matches_the_real_call():
    engine, ledger = _engine(_HonestWorker)
    _run(engine)
    assert _ledger_cents(ledger) == estimate_cost_cents(CHEAP, 1000, 500)


def test_under_reporting_is_logged(caplog):
    import logging

    engine, ledger = _engine(_LyingWorker)
    with caplog.at_level(logging.WARNING):
        _run(engine)
    assert any("metered" in r.message and "task-1" in r.message for r in caplog.records), \
        "a worker under-reporting its spend should leave a visible record"


def test_a_worker_making_no_model_call_falls_back_to_its_report():
    """The meter only overrides when it saw calls, so a stub still prices normally."""
    engine, ledger = _engine(_SilentWorker)
    _run(engine)
    assert _ledger_cents(ledger) == estimate_cost_cents(CHEAP, 800, 200)


def test_the_meter_does_not_leak_between_tasks():
    """Each task opens its own accumulator; one task's spend is not another's."""
    engine, ledger = _engine(_HonestWorker)
    _run(engine)
    first = _ledger_cents(ledger)
    _run(engine)
    assert _ledger_cents(ledger) == 2 * first, "usage from the first task leaked into the second"


def test_a_worker_cannot_reach_the_accumulator_to_forge_it():
    """
    record() is a no-op outside a metered task, so a worker calling it on its own --
    outside execute, or after the context closed -- changes nothing that gets billed.
    """
    assert usage_meter._current.get() is None
    usage_meter.record(EXPENSIVE, 9999, 9999)   # no task open
    assert usage_meter._current.get() is None
