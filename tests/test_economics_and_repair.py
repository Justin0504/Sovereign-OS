"""
Tests for autonomous-profitability + quality features:
  - governance/economics.py opportunity screening
  - ingest profit pre-screen gate
  - engine reactive self-repair loop
"""

import pytest

from sovereign_os.governance.economics import (
    complexity_from_goal,
    estimate_task_cost_cents,
    evaluate_opportunity,
    screen_task,
)


# ------------------------------------------------------------------- economics
def test_profitable_task_is_taken():
    o = evaluate_opportunity(5000, 200, fee_ratio=0.029, gas_cents=5, margin_floor=0.3)
    assert o.take is True
    assert o.net_margin_cents == 5000 - int(5000 * 0.029) - 5 - 200
    assert o.margin_ratio > 0.3


def test_unprofitable_when_cost_exceeds_payout():
    o = evaluate_opportunity(100, 200)
    assert o.take is False and o.net_margin_cents < 0
    assert "unprofitable" in o.reason


def test_gas_and_fee_can_flip_a_thin_task():
    # 100c payout, 20c LLM, but 90c gas -> net negative
    o = evaluate_opportunity(100, 20, gas_cents=90)
    assert o.take is False and o.net_margin_cents == -10


def test_margin_floor_rejects_thin_positive_margin():
    # net is positive but below the 50% floor
    o = evaluate_opportunity(100, 60, margin_floor=0.5)
    assert o.take is False and 0 < o.net_margin_cents < 50
    assert "below margin floor" in o.reason


def test_free_task_taken_only_without_floor():
    assert evaluate_opportunity(0, 500, margin_floor=0.0).take is True
    assert evaluate_opportunity(0, 500, margin_floor=0.3).take is False


def test_complexity_grows_with_length_and_parts():
    assert complexity_from_goal("hi") < complexity_from_goal("x" * 1500 + "\nand more\nand more")
    assert complexity_from_goal("") <= 1.0


def test_estimate_cost_scales_by_category():
    coding = estimate_task_cost_cents("coding")
    email = estimate_task_cost_cents("email")
    assert coding > email > 0


def test_screen_task_reads_env(monkeypatch):
    monkeypatch.setenv("SOVEREIGN_SETTLEMENT_FEE_RATIO", "0.10")
    monkeypatch.setenv("SOVEREIGN_GAS_COST_CENTS", "10")
    monkeypatch.setenv("SOVEREIGN_MIN_MARGIN_RATIO", "0.2")
    o = screen_task(10000, "Write a short blog post", "writing")
    assert o.fee_cents == 1000 and o.gas_cents == 10
    assert o.take is True  # $100 payout easily clears a $ cost + 10% fee + 20% floor


# --------------------------------------------------------- ingest profit screen
def test_ingest_profit_screen_gate(monkeypatch):
    from sovereign_os.ingest_bridge.runner import _profit_screen
    from sovereign_os.ingest_bridge.sources.base import RawOrder

    cheap = RawOrder(source_id="s1", goal="Fix a complex bug and add tests", amount_cents=5)
    rich = RawOrder(source_id="s2", goal="Write a short summary", amount_cents=5000)

    # Off by default -> everything passes
    monkeypatch.delenv("SOVEREIGN_PROFIT_SCREEN", raising=False)
    assert _profit_screen(cheap)[0] is True

    # On -> a 5-cent coding bounty is unprofitable and gets screened out
    monkeypatch.setenv("SOVEREIGN_PROFIT_SCREEN", "true")
    assert _profit_screen(cheap)[0] is False
    assert _profit_screen(rich)[0] is True


# ------------------------------------------------------------- reactive repair
class _RepairJudge:
    """Fails the first attempt; passes any corrective retry (task_id contains 'retry')."""

    _model = "repair-judge"

    async def evaluate(self, task_id, task_output, verification_prompt, kpi_name, *, min_score=None, category=None):
        from sovereign_os.auditor.base import AuditReport
        passed = "retry" in task_id
        return AuditReport(task_id=task_id, kpi_name="d", passed=passed,
                           score=0.9 if passed else 0.2,
                           reason="fixed" if passed else "needs fix",
                           suggested_fix="" if passed else "handle the edge case")


def _engine_with(judge, revenue=5000):
    from sovereign_os.agents.auth import SovereignAuth
    from sovereign_os.auditor import ReviewEngine
    from sovereign_os.governance.engine import GovernanceEngine
    from sovereign_os.ledger.unified_ledger import UnifiedLedger
    from sovereign_os.models.charter import Charter

    led = UnifiedLedger(); led.record_usd(revenue)
    charter = Charter(mission="m")
    return GovernanceEngine(charter, led, auth=SovereignAuth(),
                            review_engine=ReviewEngine(charter, judge=judge))


@pytest.mark.asyncio
async def test_repair_disabled_leaves_failure():
    engine = _engine_with(_RepairJudge())
    _, _, reports = await engine.run_mission_with_audit("Fix a bug", abort_on_audit_failure=False, max_repair_attempts=0)
    assert not all(r.passed for r in reports)


@pytest.mark.asyncio
async def test_repair_recovers_failed_task():
    events = []
    engine = _engine_with(_RepairJudge())
    engine._on_event = lambda e, d: events.append((e, d))
    _, _, reports = await engine.run_mission_with_audit("Fix a bug", abort_on_audit_failure=False, max_repair_attempts=2)
    assert all(r.passed for r in reports)
    repaired = [d for e, d in events if e == "task_repaired"]
    assert repaired and repaired[0]["passed"] is True


@pytest.mark.asyncio
async def test_repair_is_bounded_and_never_raises():
    class AlwaysFail:
        _model = "x"
        async def evaluate(self, task_id, task_output, verification_prompt, kpi_name, *, min_score=None, category=None):
            from sovereign_os.auditor.base import AuditReport
            return AuditReport(task_id=task_id, kpi_name="d", passed=False, score=0.1,
                               reason="no", suggested_fix="try harder")

    engine = _engine_with(AlwaysFail())
    _, _, reports = await engine.run_mission_with_audit("Fix a bug", abort_on_audit_failure=False, max_repair_attempts=2)
    assert not all(r.passed for r in reports)  # still fails, but no exception
