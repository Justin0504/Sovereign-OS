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
    # Post reserves the funds at funding time...
    broker.post_governed_task(title="Write a summary", description="d", price_cents=2000)
    assert led.total_usd_cents() == 8000                 # reserved
    res = await broker.review_and_settle(
        escrow_id="e1", deliverable="A solid, on-topic deliverable.",
        task_description="Write a summary", price_cents=2000,
    )
    assert res["action"] == "released" and res["paid"] is True
    assert "complete" in _kinds(spy) and "release" in _kinds(spy)
    assert led.total_usd_cents() == 8000                 # release keeps the reservation, no double debit


@pytest.mark.asyncio
async def test_quality_gate_fail_disputes_and_refunds(charter, review_engine):
    led = UnifiedLedger()
    led.record_usd(10000)
    spy = SpyClient()
    broker = OversightBroker(Treasury(charter, led), review_engine, spy, ledger=led)
    broker.post_governed_task(title="Write a summary", description="d", price_cents=2000)
    assert led.total_usd_cents() == 8000                 # reserved
    res = await broker.review_and_settle(
        escrow_id="e1", deliverable="",   # empty -> StubAuditor fails it
        task_description="Write a summary", price_cents=2000,
    )
    assert res["action"] == "disputed" and res["paid"] is False
    assert "dispute" in _kinds(spy)
    assert led.total_usd_cents() == 10000                # disputed -> reservation refunded


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


# ------------------------------------------------ registry + poller (auto-settle)
def test_registry_records_and_persists(charter, review_engine, tmp_path):
    from sovereign_os.oversight.registry import OversightRegistry

    led = UnifiedLedger(); led.record_usd(10000)
    reg = OversightRegistry(persist_path=tmp_path / "esc.json")
    broker = OversightBroker(Treasury(charter, led), review_engine, SpyClient(), ledger=led, registry=reg)
    broker.post_governed_task(title="T", description="d", price_cents=2000)
    assert reg.summary().get("funded") == 1
    # Reload from disk.
    reg2 = OversightRegistry(persist_path=tmp_path / "esc.json")
    assert reg2.summary().get("funded") == 1


@pytest.mark.asyncio
async def test_poll_and_settle_closes_the_loop(charter, review_engine):
    from sovereign_os.oversight.registry import OversightRegistry
    from sovereign_os.oversight.poller import poll_and_settle

    led = UnifiedLedger(); led.record_usd(10000)
    reg = OversightRegistry()
    # Dry-run client: get_escrow() returns status "delivered", so the poll settles.
    broker = OversightBroker(Treasury(charter, led), review_engine,
                             RentAHumanClient("", live=False), ledger=led, registry=reg)
    broker.post_governed_task(title="Good gig", description="d", price_cents=2000)
    settled = await poll_and_settle(broker, reg)
    assert len(settled) == 1 and settled[0]["action"] == "released" and settled[0]["paid"] is True
    assert reg.list(status="released")          # registry advanced
    assert led.total_usd_cents() == 8000        # paid $20
