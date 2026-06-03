"""
Tests for the x402/USDC payment rail and the governance hardening:
graduated spend permissions, settlement-fee-aware profitability,
runway-floor guard, and value-aware audit thresholds.
"""

import pytest

from sovereign_os.agents.auth import Capability, SovereignAuth
from sovereign_os.auditor.review_engine import StubAuditor, value_aware_min_score
from sovereign_os.governance.exceptions import FiscalInsolvencyError, UnprofitableJobError
from sovereign_os.governance.strategist import PlannedTask
from sovereign_os.agents.base import TaskResult
from sovereign_os.governance.treasury import Treasury
from sovereign_os.ledger.unified_ledger import UnifiedLedger
from sovereign_os.models.charter import Charter, FiscalBoundaries
from sovereign_os.payments.x402 import (
    X402PaymentService,
    cents_to_usdc_atomic,
)


# --------------------------------------------------------------------------- x402
def test_cents_to_usdc_atomic():
    assert cents_to_usdc_atomic(100) == 1_000_000   # $1.00 = 1 USDC = 1e6 atomic
    assert cents_to_usdc_atomic(1) == 10_000


@pytest.mark.asyncio
async def test_x402_sandbox_charge_is_deterministic_and_idempotent():
    svc = X402PaymentService(pay_to="0xabc", network="base-sepolia", sandbox=True)
    ref1 = await svc.charge(500, "usd", metadata={"job_id": "job-1"})
    ref2 = await svc.charge(500, "usd", metadata={"job_id": "job-1"})
    assert ref1 == ref2  # same job collapses to the same settlement ref
    assert ref1.startswith("x402_base-sepolia_sbx_0x")
    other = await svc.charge(500, "usd", metadata={"job_id": "job-2"})
    assert other != ref1  # different job -> different ref


@pytest.mark.asyncio
async def test_x402_sandbox_never_goes_live_without_facilitator():
    svc = X402PaymentService(pay_to="0xabc", sandbox=False)  # no facilitator url
    assert svc.is_live is False
    ref = await svc.charge(100, "usd")
    assert "sbx" in ref  # falls back to sandbox settlement, no network


def test_x402_from_env(monkeypatch):
    monkeypatch.setenv("X402_PAY_TO", "0xdead")
    monkeypatch.setenv("X402_NETWORK", "base")
    monkeypatch.setenv("X402_SANDBOX", "false")
    svc = X402PaymentService.from_env()
    assert svc.pay_to == "0xdead"
    assert svc.network == "base"
    assert svc.sandbox is False


def test_factory_selects_x402(monkeypatch):
    from sovereign_os.payments.service import create_payment_service

    monkeypatch.setenv("PAYMENT_PROVIDER", "x402")
    monkeypatch.setenv("X402_PAY_TO", "0xabc")
    svc = create_payment_service()
    assert isinstance(svc, X402PaymentService)


def test_factory_autoselects_x402_when_payto_set(monkeypatch):
    from sovereign_os.payments.service import create_payment_service

    monkeypatch.delenv("PAYMENT_PROVIDER", raising=False)
    monkeypatch.delenv("STRIPE_API_KEY", raising=False)
    monkeypatch.setenv("X402_PAY_TO", "0xabc")
    assert isinstance(create_payment_service(), X402PaymentService)


# ---------------------------------------------------------------- permissions
def test_graduated_spend_ceiling_scales_with_trust():
    auth = SovereignAuth(
        autonomous_spend_min_cents=100,
        autonomous_spend_max_cents=5000,
    )
    a = "agent-x"
    # Default trust 50 < SPEND_USD threshold 80 -> no autonomous spend.
    assert auth.max_spend_cents_for(a) == 0
    assert auth.can_spend(a, 50) is False

    # Climb to exactly the threshold (80) -> minimum ceiling.
    while auth.get_trust_score(a) < 80:
        auth.record_audit_success(a)
    assert auth.get_trust_score(a) >= 80
    assert auth.max_spend_cents_for(a) >= 100
    assert auth.can_spend(a, 100) is True

    # Max out trust -> max ceiling.
    while auth.get_trust_score(a) < 100:
        auth.record_audit_success(a)
    assert auth.max_spend_cents_for(a) == 5000
    assert auth.can_spend(a, 5000) is True
    assert auth.can_spend(a, 5001) is False


def test_score_scaled_trust_delta():
    auth = SovereignAuth(audit_success_delta=10, audit_failure_delta=-20)
    strong, weak = "strong", "weak"
    auth.record_audit(strong, passed=True, score=1.0)
    auth.record_audit(weak, passed=True, score=0.5)
    # Strong pass earns the full delta; marginal pass earns less.
    assert auth.get_trust_score(strong) - 50 == 10
    assert auth.get_trust_score(weak) - 50 == 5

    bad = "bad"
    auth.record_audit(bad, passed=False, score=0.0)  # full penalty
    assert auth.get_trust_score(bad) == 30


def test_trust_persistence(tmp_path):
    p = tmp_path / "trust.json"
    auth = SovereignAuth(persist_path=p)
    auth.record_audit_success("agent-1")
    score = auth.get_trust_score("agent-1")
    # Reload from disk in a fresh instance.
    auth2 = SovereignAuth(persist_path=p)
    assert auth2.get_trust_score("agent-1") == score
    assert auth2.history("agent-1")["success"] == 1


# ------------------------------------------------------------------- treasury
def _charter(**fb) -> Charter:
    return Charter(mission="m", fiscal_boundaries=FiscalBoundaries(**fb))


def test_settlement_fee_aware_profitability():
    led = UnifiedLedger()
    led.record_usd(100_000)
    # 35% margin floor + 10% settlement fee. Revenue $1.00 (100c) -> net 90c,
    # max cost = 90 * 0.65 = 58c. A 60c cost must be rejected.
    t = Treasury(_charter(min_job_margin_ratio=0.35, settlement_fee_ratio=0.10), led)
    with pytest.raises(UnprofitableJobError):
        t.approve_job_profitability(job_revenue_cents=100, total_estimated_cost_cents=60)
    # Same job is fine at a lower cost.
    t.approve_job_profitability(job_revenue_cents=100, total_estimated_cost_cents=50)


def test_settlement_fee_alone_can_reject_when_margin_disabled():
    led = UnifiedLedger()
    led.record_usd(100_000)
    t = Treasury(_charter(min_job_margin_ratio=0.0, settlement_fee_ratio=0.20), led)
    # Net revenue 80c; cost 90c loses money even with margin floor off.
    with pytest.raises(UnprofitableJobError):
        t.approve_job_profitability(job_revenue_cents=100, total_estimated_cost_cents=90)


def test_runway_projection_and_floor_guard():
    led = UnifiedLedger()
    led.record_usd(1000)          # $10 balance
    led.record_usd(-700, purpose="spend")  # burned $7 in the trailing window
    t = Treasury(_charter(runway_floor_days=5), led)
    # ~$1/day burn over 7-day window -> ~3 days of runway on $3 balance: below floor 5.
    projected = t.projected_runway_days()
    assert projected is not None and projected < 5
    with pytest.raises(FiscalInsolvencyError):
        t.approve_task(50, task_id="t1")


def test_runway_none_when_no_burn():
    led = UnifiedLedger()
    led.record_usd(1000)
    t = Treasury(_charter(runway_floor_days=5), led)
    assert t.projected_runway_days() is None  # no debits yet
    t.approve_task(50, task_id="t1")  # floor guard skipped when runway undefined


# --------------------------------------------------------------------- audit
def test_value_aware_min_score():
    assert value_aware_min_score(None) is None
    assert value_aware_min_score(0) is None
    assert value_aware_min_score(100) == 0.5    # $1 -> floor
    assert value_aware_min_score(5000) == 0.6   # $50 -> +0.1
    assert value_aware_min_score(100_000) == 0.9  # capped


@pytest.mark.asyncio
async def test_stub_auditor_respects_value_bar():
    stub = StubAuditor()
    # Stub scores a non-empty output 0.9; a 0.95 bar must fail it.
    report = await stub.evaluate(
        task_id="t1", task_output="ok", verification_prompt="p", kpi_name="k", min_score=0.95
    )
    assert report.passed is False
    # Default (no bar) passes.
    report2 = await stub.evaluate(
        task_id="t1", task_output="ok", verification_prompt="p", kpi_name="k"
    )
    assert report2.passed is True
