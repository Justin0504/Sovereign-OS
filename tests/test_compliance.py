"""
Tests for Phase 6b compliance stubs: identity, hooks, settlement.
"""

import pytest

from sovereign_os.compliance import (
    StubIdentity,
    StubComplianceHook,
    StubOnChainSettlement,
    ComplianceResult,
)


def test_stub_identity():
    ident = StubIdentity(id="entity-1", on_chain_anchor="0xabc")
    assert ident.id == "entity-1"
    assert ident.on_chain_anchor == "0xabc"
    assert ident.to_dict() == {"id": "entity-1", "on_chain_anchor": "0xabc"}


def test_stub_identity_default():
    ident = StubIdentity()
    assert ident.id == "sovereign-stub-1"
    assert ident.on_chain_anchor is None


def test_stub_compliance_hook():
    hook = StubComplianceHook()
    assert hook.check("SPEND_USD", {"amount_cents": 1000}) == ComplianceResult.ALLOW


def test_stub_on_chain_settlement():
    stub = StubOnChainSettlement()
    h = stub.submit_settlement("payout", 500, "job-42", destination="0x123")
    assert "stub" in h
    assert "payout" in h
    assert "job-42" in h
    assert "500" in h


def test_threshold_compliance_hook():
    from sovereign_os.compliance import ThresholdComplianceHook

    hook = ThresholdComplianceHook(spend_threshold_cents=1000)
    assert hook.check("SPEND_USD", {"amount_cents": 500}) == ComplianceResult.ALLOW
    assert hook.check("SPEND_USD", {"amount_cents": 1000}) == ComplianceResult.REQUEST_HUMAN_APPROVAL
    assert hook.check("SPEND_USD", {"amount_cents": 2000}) == ComplianceResult.REQUEST_HUMAN_APPROVAL
