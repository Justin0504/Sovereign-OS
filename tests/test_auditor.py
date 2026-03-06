"""Tests for Auditor (StubAuditor, ReviewEngine, verifiable audit trail)."""

import tempfile
from pathlib import Path

import pytest

from sovereign_os.agents.base import TaskResult
from sovereign_os.auditor import (
    AuditReport,
    ReviewEngine,
    StubAuditor,
    append_audit_report,
    compute_audit_proof_hash,
    load_audit_trail,
    verify_report_integrity,
)
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
    assert report.proof_hash
    assert len(report.proof_hash) == 64  # SHA-256 hex


def test_audit_report_proof_hash():
    r = AuditReport(task_id="t1", kpi_name="k1", passed=True, score=0.9)
    assert r.proof_hash
    assert compute_audit_proof_hash(r) == r.proof_hash


def test_audit_trail_append_and_load():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "audit.jsonl"
        r = AuditReport(task_id="t1", kpi_name="k1", passed=True, score=0.9)
        append_audit_report(path, r)
        entries = load_audit_trail(path)
        assert len(entries) == 1
        assert entries[0]["task_id"] == "t1"
        assert entries[0]["proof_hash"]
        assert verify_report_integrity(entries[0]) is True
