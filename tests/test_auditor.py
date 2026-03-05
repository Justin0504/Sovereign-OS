"""Tests for Auditor (StubAuditor, ReviewEngine)."""

import pytest

from sovereign_os.agents.base import TaskResult
from sovereign_os.auditor import ReviewEngine, StubAuditor
from sovereign_os.governance.strategist import PlannedTask


@pytest.mark.asyncio
async def test_stub_auditor_passes_non_empty_output():
    stub = StubAuditor()
    report = await stub.evaluate(
        task_id="t1",
        task_output="Done.",
        verification_prompt="Any prompt",
        kpi_name="k1",
    )
    assert report.passed is True
    assert report.score == 0.9


@pytest.mark.asyncio
async def test_stub_auditor_fails_empty_output():
    stub = StubAuditor()
    report = await stub.evaluate(
        task_id="t1",
        task_output="",
        verification_prompt="Any",
        kpi_name="k1",
    )
    assert report.passed is False
    assert report.score == 0.0


@pytest.mark.asyncio
async def test_review_engine_audit_task(charter, review_engine):
    task = PlannedTask(
        task_id="task-1",
        description="Research X",
        dependencies=[],
        required_skill="research",
        estimated_token_budget=500,
        priority="high",
    )
    result = TaskResult(task_id="task-1", success=True, output="Summary of X.")
    report = await review_engine.audit_task(task, result)
    assert report.task_id == "task-1"
    assert report.passed is True
