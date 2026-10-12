"""
Tests for authority on the escrow path.

This was the last ungoverned delegation point, and the most consequential of the three.
The others spend compute; this one moves real money to a real third party and funds an
escrow, which is the single place where stopping the system does not undo the mistake.

It had a CFO budget gate — "can we afford it" — and no authority check at all, so a
read-only research task could fund a bounty as readily as a task approved to spend.
"""

import pytest

from sovereign_os.agents.auth import Capability, SovereignAuth
from sovereign_os.agents.delegation import DelegationBroker
from sovereign_os.agents.delegation_gate import set_broker, task_authority
from sovereign_os.ledger.unified_ledger import UnifiedLedger
from sovereign_os.models.charter import Charter, FiscalBoundaries
from sovereign_os.oversight.broker import OversightBroker

READ = Capability.READ_FILES
SPEND = Capability.SPEND_USD


class FakeEscrowClient:
    """Records what it was asked to do, so a refusal can be told from a silent no-op."""

    def __init__(self):
        self.posted, self.funded, self.released = [], [], []

    def post_bounty(self, *, title, description, price_cents, completion_criteria=""):
        self.posted.append(title)
        return {"id": f"bounty-{len(self.posted)}"}

    def fund_escrow(self, bounty_id, amount_cents):
        self.funded.append((bounty_id, amount_cents))
        return {"id": f"escrow-{len(self.funded)}"}

    def complete(self, escrow_id):
        return {"id": escrow_id, "status": "completed"}

    def release(self, escrow_id):
        self.released.append(escrow_id)
        return {"id": escrow_id, "status": "released"}

    def dispute(self, escrow_id):
        return {"id": escrow_id, "status": "disputed"}

    def cancel(self, escrow_id):
        return {"id": escrow_id, "status": "cancelled"}


@pytest.fixture(autouse=True)
def clean_broker():
    set_broker(None)
    yield
    set_broker(None)


@pytest.fixture
def setup():
    auth = SovereignAuth()
    auth._set_score("worker-1", 95)          # eligible for everything, including SPEND_USD
    delegation = DelegationBroker(eligibility=auth.check_permission_for)
    set_broker(delegation)

    ledger = UnifiedLedger()
    ledger.record_usd(100_000, purpose="seed")
    charter = Charter(mission="escrow authority test",
                      fiscal_boundaries=FiscalBoundaries())
    client = FakeEscrowClient()
    from sovereign_os.auditor.review_engine import StubAuditor
    from sovereign_os.auditor import ReviewEngine
    from sovereign_os.governance.treasury import Treasury

    broker = OversightBroker(
        Treasury(charter, ledger),
        ReviewEngine(charter, judge=StubAuditor()),
        client,
        ledger=ledger,
    )
    return broker, client, delegation


def _post(broker, price_cents=500):
    return broker.post_governed_task(
        title="Fix the flaky integration test", description="…",
        price_cents=price_cents, required_skill="coding")


# ---------------------------------------------------- the hole, closed

def test_a_task_without_spend_authority_cannot_fund_an_escrow(setup):
    broker, client, delegation = setup
    grant = delegation.root("worker-1", task_id="t1", capabilities={READ},
                            budget_cents=10_000)

    with task_authority(grant.grant_id):
        result = _post(broker)

    assert result["posted"] is False
    assert "spend_usd" in result["reason"]
    # The refusal has to happen before money moves, not after.
    assert client.posted == [] and client.funded == []


def test_a_task_with_spend_authority_can(setup):
    broker, client, delegation = setup
    grant = delegation.root("worker-1", task_id="t1", capabilities={SPEND},
                            budget_cents=10_000)

    with task_authority(grant.grant_id):
        result = _post(broker, price_cents=500)

    assert result["posted"] is True
    assert client.funded == [("bounty-1", 500)]


def test_the_refusal_is_recorded_not_swallowed(setup):
    """An operator has to be able to see that a post was refused and why."""
    broker, client, delegation = setup
    grant = delegation.root("worker-1", task_id="t1", capabilities={READ},
                            budget_cents=10_000)
    with task_authority(grant.grant_id):
        _post(broker)

    with task_authority(grant.grant_id):
        result = _post(broker)
    # The reason is the contract: a refusal that returns an empty string is
    # indistinguishable to the caller from a platform outage.
    assert result["reason"] and "spend_usd" in result["reason"]
    assert result["posted"] is False


# ------------------------------------------- budget conservation across posts

def test_several_posts_cannot_collectively_exceed_the_task(setup):
    """
    Each post passes its own CFO check — the ledger can afford all of them — but the
    hiring task was approved for less. Reserving from the grant is what stops the
    subtree outspending its root, which the per-post check alone cannot.
    """
    broker, client, delegation = setup
    grant = delegation.root("worker-1", task_id="t1", capabilities={SPEND},
                            budget_cents=1_000)

    with task_authority(grant.grant_id):
        first = _post(broker, price_cents=600)
        second = _post(broker, price_cents=600)

    assert first["posted"] is True and first["grant_id"]
    # The ledger could afford the second; the task's own grant could not.
    assert delegation.get(grant.grant_id).available_cents == 400
    assert second["grant_id"] == "", "the second post must not carve a grant it cannot fund"


def test_a_funded_post_reserves_from_the_tasks_grant(setup):
    broker, client, delegation = setup
    grant = delegation.root("worker-1", task_id="t1", capabilities={SPEND},
                            budget_cents=5_000)

    with task_authority(grant.grant_id):
        result = _post(broker, price_cents=2_000)

    child = delegation.get(result["grant_id"])
    assert child.budget_cents == 2_000
    assert child.principal_chain == ("worker-1", f"escrow:{result['escrow_id']}")
    assert delegation.get(grant.grant_id).available_cents == 3_000


# ------------------------------------------------- posture and compatibility

def test_without_a_broker_the_historical_behaviour_is_preserved(setup):
    """A self-host that never had delegation governance keeps working."""
    broker, client, _ = setup
    set_broker(None)
    result = _post(broker, price_cents=500)
    assert result["posted"] is True
    assert client.funded == [("bounty-1", 500)]


def test_strict_mode_refuses_an_ungoverned_post(setup, monkeypatch):
    broker, client, _ = setup
    monkeypatch.setenv("SOVEREIGN_STRICT_DELEGATION", "1")
    result = _post(broker, price_cents=500)        # no task authority bound
    assert result["posted"] is False
    assert client.funded == []


def test_the_budget_gate_still_applies_on_top(setup):
    """Authority is additional to affordability, not a replacement for it."""
    broker, client, delegation = setup
    grant = delegation.root("worker-1", task_id="t1", capabilities={SPEND},
                            budget_cents=10_000_000)
    with task_authority(grant.grant_id):
        result = _post(broker, price_cents=9_000_000)   # more than the ledger holds
    assert result["posted"] is False
    assert client.funded == []
