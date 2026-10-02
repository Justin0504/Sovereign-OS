"""
Adversarial tests for governed delegation.

These are the attacks from `bench.threat_model` aimed at the layer that was missing when
that threat model was written. PE-04 ("delegated execution") was flagged there as the
highest-value one, because it is where a governance layer that wraps the *planner*
instead of the *effects* fails structurally — and the system really did have that hole:
`CodeAssistantWorker._maybe_delegate` hands a whole task and a workspace path to an
external coding agent, and the authority layer had no concept of delegation at all.

The tests are written as attacks rather than as feature checks on purpose. "A child
cannot exceed its parent" is a property that has to hold against someone trying, not a
behaviour to demonstrate once.
"""

import pytest

from sovereign_os.agents.auth import Capability
from sovereign_os.agents.delegation import (
    AttenuationError,
    BudgetExceeded,
    DelegationBroker,
    DepthExceeded,
    GrantNotLive,
)

READ = Capability.READ_FILES
WRITE = Capability.WRITE_FILES
SHELL = Capability.EXECUTE_SHELL
SPEND = Capability.SPEND_USD


@pytest.fixture
def broker():
    return DelegationBroker()


@pytest.fixture
def root(broker):
    return broker.root("planner", task_id="t1", capabilities={READ, WRITE},
                       budget_cents=1000)


# ------------------------------------------------- PE-04: delegated escalation

def test_a_child_cannot_be_given_what_the_parent_lacks(broker, root):
    """The core invariant. Authority only ever narrows as it is handed on."""
    with pytest.raises(AttenuationError) as e:
        broker.attenuate(root.grant_id, "coder", capabilities={READ, SHELL},
                         budget_cents=100)
    assert "execute_shell" in str(e.value)


def test_authority_cannot_be_regained_further_down_the_chain(broker, root):
    """
    Laundering attempt: narrow to READ, then try to widen back to WRITE two hops later.
    Each step must be checked against its own parent, not against the root.
    """
    mid = broker.attenuate(root.grant_id, "mid", capabilities={READ}, budget_cents=500)
    with pytest.raises(AttenuationError):
        broker.attenuate(mid.grant_id, "leaf", capabilities={READ, WRITE}, budget_cents=100)


def test_a_highly_trusted_child_still_cannot_exceed_its_grant():
    """
    The confused deputy. The callee is genuinely eligible for SHELL; the chain is not.
    Checking the callee's own standing — the intuitive implementation — permits exactly
    this, which is why effective authority is the INTERSECTION of grant and eligibility.
    """
    broker = DelegationBroker(eligibility=lambda agent, cap: True)   # everyone eligible
    root = broker.root("planner", task_id="t1", capabilities={READ}, budget_cents=100)
    with pytest.raises(AttenuationError):
        broker.attenuate(root.grant_id, "trusted-coder", capabilities={SHELL},
                         budget_cents=10)


def test_a_grant_cannot_lift_an_ineligible_holder(broker):
    """The other half of the intersection: a grant is not a promotion."""
    def eligibility(agent, cap):
        return not (agent == "junior" and cap is WRITE)

    broker = DelegationBroker(eligibility=eligibility)
    root = broker.root("planner", task_id="t1", capabilities={READ, WRITE},
                       budget_cents=100)
    child = broker.attenuate(root.grant_id, "junior", capabilities={READ, WRITE},
                             budget_cents=10)
    assert child.capabilities == frozenset({READ})       # narrowed, not refused
    with pytest.raises(GrantNotLive):
        broker.authorize(child.grant_id, WRITE)


def test_eligibility_is_rechecked_at_use_not_only_at_issue():
    """Trust lost mid-task must bite immediately, not at the next handoff."""
    revoked = set()
    broker = DelegationBroker(eligibility=lambda a, c: (a, c) not in revoked)
    root = broker.root("planner", task_id="t1", capabilities={WRITE}, budget_cents=100)
    assert broker.authorize(root.grant_id, WRITE)
    revoked.add(("planner", WRITE))
    with pytest.raises(GrantNotLive):
        broker.authorize(root.grant_id, WRITE)


def test_a_failing_eligibility_check_denies(monkeypatch):
    """An erroring check must never read as permission."""
    def boom(agent, cap):
        raise RuntimeError("trust store down")

    broker = DelegationBroker(eligibility=boom)
    root = broker.root("planner", task_id="t1", capabilities={WRITE}, budget_cents=10)
    assert root.capabilities == frozenset()


def test_strict_mode_refuses_to_run_without_an_eligibility_check():
    with pytest.raises(ValueError):
        DelegationBroker(strict_eligibility=True)


# ------------------------------------------------- BE-01 applied to delegation

def test_budget_is_drawn_from_the_parent_not_reissued(broker, root):
    """
    Task splitting, one level up. Each child's budget is individually modest; without
    conservation the subtree total is unbounded.
    """
    broker.attenuate(root.grant_id, "a", capabilities={READ}, budget_cents=600)
    with pytest.raises(BudgetExceeded) as e:
        broker.attenuate(root.grant_id, "b", capabilities={READ}, budget_cents=600)
    assert "400c still" in str(e.value)


def test_a_child_spend_rolls_up_to_the_root(broker, root):
    """
    Consumption is derived from the tree rather than denormalised into each ancestor,
    so a rolled-up figure cannot drift from the per-grant records.
    """
    mid = broker.attenuate(root.grant_id, "mid", capabilities={READ}, budget_cents=500)
    leaf = broker.attenuate(mid.grant_id, "leaf", capabilities={READ}, budget_cents=200)

    broker.spend(leaf.grant_id, 150)

    assert broker.get(leaf.grant_id).direct_spent_cents == 150
    assert broker.get(mid.grant_id).direct_spent_cents == 0, "mid did no work itself"
    assert broker.consumed_cents(root.grant_id) == 150, "the root is the real ceiling"


def test_the_subtree_total_is_bounded_by_the_root(broker, root):
    """However wide it grows, the tree cannot outspend what the root was given."""
    kids = [broker.attenuate(root.grant_id, f"k{i}", capabilities={READ},
                             budget_cents=250) for i in range(4)]
    for k in kids:
        broker.spend(k.grant_id, 250)
    assert broker.consumed_cents(root.grant_id) == 1000
    assert broker.get(root.grant_id).available_cents == 0

    # Fully committed: nothing left to delegate, and no room to spend directly.
    with pytest.raises(BudgetExceeded):
        broker.attenuate(root.grant_id, "late", capabilities={READ}, budget_cents=1)
    with pytest.raises(BudgetExceeded):
        broker.spend(root.grant_id, 1)


def test_a_refused_spend_leaves_no_partial_debit(broker, root):
    """Check the whole chain before applying any of it."""
    mid = broker.attenuate(root.grant_id, "mid", capabilities={READ}, budget_cents=100)
    leaf = broker.attenuate(mid.grant_id, "leaf", capabilities={READ}, budget_cents=100)
    with pytest.raises(BudgetExceeded):
        broker.spend(leaf.grant_id, 150)
    assert broker.get(leaf.grant_id).direct_spent_cents == 0
    assert broker.consumed_cents(root.grant_id) == 0


def test_spending_through_a_revoked_ancestor_is_refused(broker, root):
    mid = broker.attenuate(root.grant_id, "mid", capabilities={READ}, budget_cents=500)
    leaf = broker.attenuate(mid.grant_id, "leaf", capabilities={READ}, budget_cents=100)
    broker.revoke(mid.grant_id)
    with pytest.raises(GrantNotLive):
        broker.spend(leaf.grant_id, 10)


# ------------------------------------------------- runaway recursion

def test_depth_is_bounded():
    broker = DelegationBroker(max_depth=2)
    g = broker.root("a0", task_id="t", capabilities={READ}, budget_cents=1000)
    g = broker.attenuate(g.grant_id, "a1", capabilities={READ}, budget_cents=100)
    g = broker.attenuate(g.grant_id, "a2", capabilities={READ}, budget_cents=50)
    with pytest.raises(DepthExceeded):
        broker.attenuate(g.grant_id, "a3", capabilities={READ}, budget_cents=10)


def test_fanout_is_bounded():
    broker = DelegationBroker(max_fanout=3)
    root = broker.root("p", task_id="t", capabilities={READ}, budget_cents=1000)
    for i in range(3):
        broker.attenuate(root.grant_id, f"c{i}", capabilities={READ}, budget_cents=10)
    with pytest.raises(DepthExceeded):
        broker.attenuate(root.grant_id, "c3", capabilities={READ}, budget_cents=10)


# ------------------------------------------------- PE-01/PE-02: lifetime

def test_revocation_cascades_to_descendants(broker, root):
    """A child's authority is a slice of its parent's; it cannot outlive it."""
    mid = broker.attenuate(root.grant_id, "mid", capabilities={READ}, budget_cents=500)
    leaf = broker.attenuate(mid.grant_id, "leaf", capabilities={READ}, budget_cents=100)

    revoked = broker.revoke(mid.grant_id)

    assert set(revoked) == {mid.grant_id, leaf.grant_id}
    assert root.grant_id not in revoked
    with pytest.raises(GrantNotLive):
        broker.authorize(leaf.grant_id, READ)
    assert broker.authorize(root.grant_id, READ)


def test_a_child_cannot_outlive_its_parents_expiry():
    """A longer child TTL would leave authority alive after its source is gone."""
    now = [1000.0]
    broker = DelegationBroker(clock=lambda: now[0])
    root = broker.root("p", task_id="t", capabilities={READ}, budget_cents=100,
                       ttl_seconds=10)
    child = broker.attenuate(root.grant_id, "c", capabilities={READ}, budget_cents=10,
                             ttl_seconds=10_000)
    assert child.expires_at == root.expires_at

    now[0] = 1011.0
    with pytest.raises(GrantNotLive):
        broker.authorize(child.grant_id, READ)


def test_revoking_a_task_tears_down_every_grant_it_issued(broker):
    r1 = broker.root("p", task_id="t1", capabilities={READ}, budget_cents=100)
    broker.attenuate(r1.grant_id, "c", capabilities={READ}, budget_cents=10)
    r2 = broker.root("p", task_id="t2", capabilities={READ}, budget_cents=100)

    revoked = broker.revoke_task("t1")

    assert len(revoked) == 2
    assert broker.authorize(r2.grant_id, READ)


# ------------------------------------------------- audit: attributing a leaf

def test_the_principal_chain_travels_with_the_authority(broker, root):
    mid = broker.attenuate(root.grant_id, "mid", capabilities={READ}, budget_cents=500)
    leaf = broker.attenuate(mid.grant_id, "external-coder", capabilities={READ},
                            budget_cents=100)
    assert leaf.principal_chain == ("planner", "mid", "external-coder")
    assert broker.chain(leaf.grant_id) == ("planner", "mid", "external-coder")


def test_the_tree_is_reconstructable_for_an_audit_record(broker, root):
    mid = broker.attenuate(root.grant_id, "mid", capabilities={READ}, budget_cents=500)
    broker.attenuate(mid.grant_id, "leaf", capabilities={READ}, budget_cents=100)

    tree = broker.tree(root.grant_id)

    assert tree["agent_id"] == "planner"
    assert tree["children"][0]["agent_id"] == "mid"
    assert tree["children"][0]["children"][0]["agent_id"] == "leaf"
    assert tree["children"][0]["children"][0]["principal_chain"] == ["planner", "mid", "leaf"]


def test_an_unknown_grant_is_never_authorized(broker):
    with pytest.raises(GrantNotLive):
        broker.authorize("grant-does-not-exist", READ)


def test_a_grant_does_not_carry_capabilities_it_was_not_given(broker, root):
    child = broker.attenuate(root.grant_id, "c", capabilities={READ}, budget_cents=10)
    assert broker.authorize(child.grant_id, READ)
    with pytest.raises(GrantNotLive):
        broker.authorize(child.grant_id, WRITE)


# ------------------------------------------------- integration with SovereignAuth

def test_real_trust_scores_drive_eligibility():
    """The intersection rule against the actual authority store."""
    from sovereign_os.agents.auth import SovereignAuth

    auth = SovereignAuth()
    auth._set_score("planner", 95)      # eligible for everything
    auth._set_score("junior", 15)       # READ only
    broker = DelegationBroker(eligibility=auth.check_permission_for,
                              strict_eligibility=True)

    root = broker.root("planner", task_id="t1", capabilities={READ, WRITE, SHELL},
                       budget_cents=500)
    assert root.capabilities == frozenset({READ, WRITE, SHELL})

    junior = broker.attenuate(root.grant_id, "junior", capabilities={READ, WRITE},
                              budget_cents=100)
    assert junior.capabilities == frozenset({READ}), "junior has not earned WRITE"
    with pytest.raises(GrantNotLive):
        broker.authorize(junior.grant_id, WRITE)
    assert broker.authorize(junior.grant_id, READ)


def test_delegating_reserves_immediately_not_on_first_spend(broker, root):
    """
    The specific hole the commitment model closes: delegation itself has to consume the
    parent's headroom. Checking only "what has been spent" lets two 600c delegations out
    of a 1000c budget both pass, because neither has spent anything yet.
    """
    assert broker.get(root.grant_id).available_cents == 1000
    broker.attenuate(root.grant_id, "a", capabilities={READ}, budget_cents=600)
    assert broker.get(root.grant_id).available_cents == 400
    assert broker.consumed_cents(root.grant_id) == 0, "reserved is not spent"


def test_a_parent_cannot_spend_what_it_delegated_away(broker, root):
    """Reservations bind the parent too, or the child's budget is not really theirs."""
    broker.attenuate(root.grant_id, "a", capabilities={READ}, budget_cents=900)
    broker.spend(root.grant_id, 100)
    with pytest.raises(BudgetExceeded):
        broker.spend(root.grant_id, 1)


def test_revoking_a_branch_returns_its_unspent_reservation(broker, root):
    """A cancelled branch must not strand budget for the rest of the mission."""
    child = broker.attenuate(root.grant_id, "a", capabilities={READ}, budget_cents=800)
    broker.spend(child.grant_id, 200)
    assert broker.get(root.grant_id).available_cents == 200

    broker.revoke(child.grant_id)

    assert broker.get(root.grant_id).available_cents == 800   # 1000 - 200 consumed
    assert broker.consumed_cents(root.grant_id) == 200, "spent money is not refunded"
