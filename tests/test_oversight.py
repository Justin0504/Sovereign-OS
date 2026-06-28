"""
Tests for the outbound oversight broker: CFO budget gate before funding,
Auditor quality gate before releasing escrow. No network (client is dry-run).
"""

import pytest

from sovereign_os.governance.treasury import Treasury
from sovereign_os.ledger.unified_ledger import UnifiedLedger
from sovereign_os.oversight.broker import OversightBroker
from sovereign_os.oversight.rentahuman import RentAHumanClient


class SpyClient:
    """EscrowClient that records calls; statuses mimic the dry-run client."""

    def __init__(self):
        self.calls = []

    def post_bounty(self, *, title, description, price_cents, completion_criteria=""):
        self.calls.append(("post", title, price_cents))
        return {"id": "b1", "status": "pending"}

    def fund_escrow(self, bounty_id, amount_cents):
        self.calls.append(("fund", bounty_id, amount_cents))
        return {"id": "e1", "status": "funded"}

    def complete(self, escrow_id):
        self.calls.append(("complete", escrow_id))
        return {"id": escrow_id, "status": "completed"}

    def release(self, escrow_id):
        self.calls.append(("release", escrow_id))
        return {"id": escrow_id, "status": "released"}

    def dispute(self, escrow_id):
        self.calls.append(("dispute", escrow_id))
        return {"id": escrow_id, "status": "disputed"}

    def cancel(self, escrow_id):
        self.calls.append(("cancel", escrow_id))
        return {"id": escrow_id, "status": "cancelled"}


def _kinds(spy):
    return [c[0] for c in spy.calls]


# ------------------------------------------------------------ budget gate
def test_budget_gate_rejects_when_insolvent(charter, review_engine):
    led = UnifiedLedger()
    led.record_usd(100)  # $1.00 balance
    broker = OversightBroker(Treasury(charter, led), review_engine, SpyClient(), ledger=led)
    res = broker.post_governed_task(title="Big task", description="d", price_cents=5000)  # $50
    assert res["posted"] is False
    assert "CFO denied" in res["reason"] or "denied" in res["reason"].lower()


def test_budget_gate_posts_and_funds_when_affordable(charter, review_engine):
    led = UnifiedLedger()
    led.record_usd(10000)  # $100 balance
    spy = SpyClient()
    broker = OversightBroker(Treasury(charter, led), review_engine, spy, ledger=led)
    res = broker.post_governed_task(title="Small task", description="d", price_cents=2000)
    assert res["posted"] is True
    assert res["escrow_id"] == "e1"
    assert _kinds(spy) == ["post", "fund"]


# ----------------------------------------------------------- quality gate
@pytest.mark.asyncio
async def test_quality_gate_pass_releases_and_records_spend(charter, review_engine):
    led = UnifiedLedger()
    led.record_usd(10000)
    spy = SpyClient()
    broker = OversightBroker(Treasury(charter, led), review_engine, spy, ledger=led)
    before = led.total_usd_cents()
    res = await broker.review_and_settle(
        escrow_id="e1", deliverable="A solid, on-topic deliverable.",
        task_description="Write a summary", price_cents=2000,
    )
    assert res["action"] == "released" and res["paid"] is True
    assert _kinds(spy) == ["complete", "release"]       # accepted then paid
    assert led.total_usd_cents() == before - 2000       # spend recorded


@pytest.mark.asyncio
async def test_quality_gate_fail_disputes_and_withholds_payment(charter, review_engine):
    led = UnifiedLedger()
    led.record_usd(10000)
    spy = SpyClient()
    broker = OversightBroker(Treasury(charter, led), review_engine, spy, ledger=led)
    before = led.total_usd_cents()
    res = await broker.review_and_settle(
        escrow_id="e1", deliverable="",   # empty -> StubAuditor fails it
        task_description="Write a summary", price_cents=2000,
    )
    assert res["action"] == "disputed" and res["paid"] is False
    assert _kinds(spy) == ["dispute"]                   # no complete/release
    assert led.total_usd_cents() == before             # nothing paid


# --------------------------------------------------------------- client
def test_client_dry_run_does_not_hit_network():
    posted = []
    c = RentAHumanClient("", live=False, post_json=lambda *a: posted.append(a))
    b = c.post_bounty(title="T", description="D", price_cents=2500)
    f = c.fund_escrow(b["id"], 2500)
    r = c.release(f["id"])
    assert b["dry_run"] and f["dry_run"] and r["dry_run"]
    assert f["status"] == "funded" and r["status"] == "released"
    assert posted == []  # no network in dry-run


def test_client_live_requires_key():
    c = RentAHumanClient("", live=True)
    with pytest.raises(ValueError):
        c.fund_escrow("b1", 2500)


def test_client_live_posts_with_header():
    calls = []

    def _post(url, body, headers, timeout):
        calls.append((url, body, headers))
        return {"success": True, "id": "x", "status": "released"}

    c = RentAHumanClient("rah_live_k", live=True, post_json=_post)
    c.release("e1")
    assert calls[0][0].endswith("/escrow/e1/release")
    assert calls[0][2]["X-API-Key"] == "rah_live_k"
