"""Tests for GovernanceEngine (run_mission, dispatch, run_mission_with_audit)."""

import pytest

from sovereign_os.agents.auth import SovereignAuth
from sovereign_os.auditor import ReviewEngine
from sovereign_os.governance.engine import GovernanceEngine
from sovereign_os.compliance import ThresholdComplianceHook
from sovereign_os.governance.exceptions import FiscalInsolvencyError, HumanApprovalRequiredError
from sovereign_os.ledger.unified_ledger import UnifiedLedger


@pytest.mark.asyncio
async def test_run_mission_produces_plan(charter, ledger, auth, review_engine):
    engine = GovernanceEngine(
        charter, ledger, auth=auth, review_engine=review_engine
    )
    plan = await engine.run_mission("Summarize the market.")
    assert plan.goal_summary
    assert len(plan.tasks) >= 1
    assert plan.tasks[0].required_skill in ("research", "code", "general")


@pytest.mark.asyncio
async def test_run_mission_denies_when_insolvent(charter, auth, review_engine):
    led = UnifiedLedger()
    led.record_usd(1)  # almost no balance
    engine = GovernanceEngine(charter, led, auth=auth, review_engine=review_engine)
    with pytest.raises(FiscalInsolvencyError):
        await engine.run_mission("Expensive multi-step goal that needs more than 1 cent.")


@pytest.mark.asyncio
async def test_dispatch_runs_stub_worker(charter, ledger, auth, review_engine):
    engine = GovernanceEngine(
        charter, ledger, auth=auth, review_engine=review_engine
    )
    plan = await engine.run_mission("Do one task.")
    results = await engine.dispatch(plan)
    assert len(results) == len(plan.tasks)
    assert all(r.task_id for r in results)
    assert results[0].success is True
    assert results[0].output


@pytest.mark.asyncio
async def test_run_mission_with_audit_full_pipeline(charter, ledger, auth, review_engine):
    engine = GovernanceEngine(
        charter, ledger, auth=auth, review_engine=review_engine
    )
    plan, results, reports = await engine.run_mission_with_audit(
        "Complete one research task.", abort_on_audit_failure=False
    )
    assert len(plan.tasks) >= 1
    assert len(results) == len(plan.tasks)
    assert len(reports) == len(plan.tasks)
    assert all(r.passed for r in reports)


@pytest.mark.asyncio
async def test_run_mission_raises_human_approval_when_above_compliance_threshold(charter, ledger, auth, review_engine):
    """When compliance hook threshold is set and task cost exceeds it, run_mission raises HumanApprovalRequiredError."""
    ledger.record_usd(5000)  # enough balance so fiscal check passes; compliance hook runs next
    hook = ThresholdComplianceHook(spend_threshold_cents=1000)
    cost_converter = lambda t: 2000
    engine = GovernanceEngine(
        charter,
        ledger,
        auth=auth,
        review_engine=review_engine,
        compliance_hook=hook,
        spend_threshold_cents=1000,
        cost_converter=cost_converter,
    )
    with pytest.raises(HumanApprovalRequiredError) as exc_info:
        await engine.run_mission("One task.")
    assert exc_info.value.amount_cents == 2000
    assert "1000" in str(exc_info.value)
