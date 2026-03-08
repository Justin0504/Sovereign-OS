"""
E2E integration test: full pipeline with mock Strategist LLM (no real API calls).
Asserts plan → dispatch → audit flow and report integrity.
"""

import os
from unittest.mock import patch

import pytest

from sovereign_os.agents.auth import SovereignAuth
from sovereign_os.auditor import ReviewEngine
from sovereign_os.governance.engine import GovernanceEngine
from sovereign_os.governance.strategist import PlannedTask, TaskPlan
from sovereign_os.ledger.unified_ledger import UnifiedLedger


class MockStrategistLLM:
    """Deterministic mock for CEO plan; no LLM calls."""

    async def plan_from_goal(self, goal: str, charter) -> TaskPlan:
        return TaskPlan(
            goal_summary=goal[:80],
            tasks=[
                PlannedTask(
                    task_id="task-1",
                    description=goal[:200] or "Single research task",
                    dependencies=[],
                    required_skill="research",
                    estimated_token_budget=500,
                    priority="high",
                ),
            ],
        )


@pytest.mark.asyncio
async def test_e2e_full_pipeline_with_mock_strategist(charter, ledger, auth, review_engine):
    """Run plan → CFO approval → dispatch → audit with mock CEO; no real LLM."""
    mock_ceo = MockStrategistLLM()
    engine = GovernanceEngine(
        charter,
        ledger,
        auth=auth,
        review_engine=review_engine,
        strategist_llm=mock_ceo,
    )
    goal = "Summarize the market in one paragraph."
    plan, results, reports = await engine.run_mission_with_audit(
        goal,
        abort_on_audit_failure=False,
    )
    assert plan.goal_summary == goal[:80]
    assert len(plan.tasks) == 1
    # _normalize_plan_task_ids rewrites task_id to "task-{n}-{skill}"
    assert plan.tasks[0].task_id == "task-1-research"
    assert plan.tasks[0].required_skill == "research"
    assert len(results) == 1
    assert results[0].task_id == "task-1-research"
    assert results[0].success is True
    assert len(results[0].output) > 0
    assert len(reports) == 1
    assert reports[0].passed is True
    assert reports[0].proof_hash
    assert len(reports[0].proof_hash) == 64


@pytest.mark.asyncio
async def test_e2e_audit_trail_persisted(charter, ledger, auth, tmp_path):
    """When audit_trail_path is set, reports are appended to JSONL."""
    trail_path = tmp_path / "audit.jsonl"
    review = ReviewEngine(charter, judge=None, audit_trail_path=str(trail_path))
    mock_ceo = MockStrategistLLM()
    engine = GovernanceEngine(
        charter,
        ledger,
        auth=auth,
        review_engine=review,
        strategist_llm=mock_ceo,
    )
    plan, results, reports = await engine.run_mission_with_audit(
        "One task.",
        abort_on_audit_failure=False,
    )
    assert trail_path.exists()
    lines = trail_path.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 1
    import json
    entry = json.loads(lines[0])
    # _normalize_plan_task_ids rewrites task_id to "task-{n}-{skill}"
    assert entry["task_id"] == "task-1-research"
    assert entry["proof_hash"]
    from sovereign_os.auditor.trail import verify_report_integrity
    assert verify_report_integrity(entry) is True


def test_default_registry_has_builtin_workers(charter):
    """Default engine registry registers built-in workers for common skills."""
    from sovereign_os.governance.engine import GovernanceEngine
    from sovereign_os.ledger.unified_ledger import UnifiedLedger

    led = UnifiedLedger()
    led.record_usd(1000)
    engine = GovernanceEngine(charter, led)
    r = engine._registry
    for skill in (
        "summarize",
        "research",
        "reply",
        "write_article",
        "solve_problem",
        "write_email",
        "write_post",
        "meeting_minutes",
        "translate",
        "rewrite_polish",
        "collect_info",
        "extract_structured",
        "spec_writer",
        "assistant_chat",
        "code_assistant",
        "code_review",
    ):
        bidders = r.get_bidders(skill)
        assert len(bidders) >= 1, f"Expected at least one bidder for skill {skill}"
        assert any(skill in bidder_id for bidder_id, _ in bidders)


def test_e2e_job_completion_fires_webhook(charter, ledger, auth, review_engine):
    """When a job completes, _fire_job_webhook calls notify_job_completion with expected payload (E2E-style: job run + webhook)."""
    from sovereign_os.web.app import Job, _fire_job_webhook

    job = Job(
        job_id=42,
        goal="Summarize X",
        charter="Default",
        amount_cents=0,
        currency="USD",
        status="completed",
        callback_url="http://test.example/hook",
    )
    # Mock result/report objects with minimal attributes used by _fire_job_webhook
    class R:
        output = "Done."
    class Rep:
        score = 0.9
    results, reports = [R()], [Rep()]
    with patch.dict(os.environ, {"SOVEREIGN_WEBHOOK_URL": ""}, clear=False):
        with patch("sovereign_os.web.job_webhook.notify_job_completion") as mock_notify:
            _fire_job_webhook(job, "completed", results, reports)
            mock_notify.assert_called_once()
            call_kw = mock_notify.call_args[1]
            assert call_kw["job_id"] == 42
            assert call_kw["status"] == "completed"
            assert "Summarize X" in call_kw["goal"]
            assert call_kw["amount_cents"] == 0
            assert call_kw["audit_score"] == 0.9
