"""
Tests for the seam between the delegation model and the code that actually hands work
to another agent.

`DelegationBroker` was correct and connected to nothing, which protects nothing. These
pin the connection, and specifically the thing that was true before it existed: a worker
could pass an entire task plus a workspace path to an external coding agent, and no
authority check happened anywhere on that path.
"""

import pytest

from sovereign_os.agents.auth import Capability, SovereignAuth
from sovereign_os.agents.delegation import DelegationBroker
from sovereign_os.agents.delegation_gate import (
    EXTERNAL_AGENT_CAPABILITIES,
    authorize_external_agent,
    delegate_budget,
    get_broker,
    release_task,
    set_broker,
)

READ = Capability.READ_FILES
WRITE = Capability.WRITE_FILES
SHELL = Capability.EXECUTE_SHELL


@pytest.fixture(autouse=True)
def clean_broker():
    set_broker(None)
    yield
    set_broker(None)


@pytest.fixture
def broker():
    b = DelegationBroker()
    set_broker(b)
    return b


def _authorize(grant_id=""):
    return authorize_external_agent(grant_id=grant_id, backend_id="claude-code",
                                    task_id="t1")


# ------------------------------------------------------- what the gate requires

def test_an_external_agent_needs_write_and_shell():
    """
    Framing matters: the authority required is to change files and run commands, not to
    "use a backend". The second framing is what lets this pass unexamined.
    """
    assert EXTERNAL_AGENT_CAPABILITIES == frozenset({WRITE, SHELL})


def test_a_grant_carrying_the_capabilities_is_authorized(broker):
    grant = broker.root("coder", task_id="t1", capabilities={READ, WRITE, SHELL},
                        budget_cents=100)
    permitted, reason = _authorize(grant.grant_id)
    assert permitted is True and reason == "authorized"


def test_a_read_only_task_cannot_reach_an_external_agent(broker):
    """The core case: a task approved to read must not get a shell by routing around."""
    grant = broker.root("researcher", task_id="t1", capabilities={READ},
                        budget_cents=100)
    permitted, reason = _authorize(grant.grant_id)
    assert permitted is False
    assert "write_files" in reason and "execute_shell" in reason


def test_a_partially_capable_grant_is_still_refused(broker):
    """WRITE without SHELL is not enough — the agent gets a free hand in the workspace."""
    grant = broker.root("coder", task_id="t1", capabilities={READ, WRITE},
                        budget_cents=100)
    permitted, reason = _authorize(grant.grant_id)
    assert permitted is False and "execute_shell" in reason


def test_a_revoked_grant_stops_authorizing(broker):
    grant = broker.root("coder", task_id="t1", capabilities={WRITE, SHELL},
                        budget_cents=100)
    assert _authorize(grant.grant_id)[0] is True
    broker.revoke(grant.grant_id)
    assert _authorize(grant.grant_id)[0] is False


def test_trust_lost_mid_task_closes_the_gate():
    """Eligibility is re-checked at use, so a demotion bites before the next handoff."""
    auth = SovereignAuth()
    auth._set_score("coder", 95)
    broker = DelegationBroker(eligibility=auth.check_permission_for)
    set_broker(broker)
    grant = broker.root("coder", task_id="t1", capabilities={WRITE, SHELL},
                        budget_cents=100)
    assert _authorize(grant.grant_id)[0] is True

    auth._set_score("coder", 10)        # demoted below the shell threshold
    assert _authorize(grant.grant_id)[0] is False


# ------------------------------------------------- permissive vs strict posture

def test_without_a_broker_the_historical_behaviour_is_preserved():
    """A self-host that never had delegation governance must keep working."""
    assert get_broker() is None
    permitted, reason = _authorize("")
    assert permitted is True and "no delegation governance" in reason


def test_strict_mode_refuses_when_no_broker_is_installed(monkeypatch):
    monkeypatch.setenv("SOVEREIGN_STRICT_DELEGATION", "1")
    permitted, reason = _authorize("")
    assert permitted is False and "no delegation broker" in reason


def test_an_ungoverned_task_passes_but_warns(broker, caplog):
    """Permissive by default, and never silently: the warning names the fix."""
    with caplog.at_level("WARNING"):
        permitted, reason = _authorize("")
    assert permitted is True and "ungoverned" in reason
    assert "SOVEREIGN_STRICT_DELEGATION" in caplog.text


def test_strict_mode_refuses_an_ungoverned_task(broker, monkeypatch):
    monkeypatch.setenv("SOVEREIGN_STRICT_DELEGATION", "1")
    permitted, reason = _authorize("")
    assert permitted is False and "carries no delegation grant" in reason


# ------------------------------------------------------------- budget handoff

def test_a_sub_agent_spends_the_tasks_budget(broker):
    grant = broker.root("coder", task_id="t1", capabilities={WRITE, SHELL},
                        budget_cents=500)
    child_id = delegate_budget(grant_id=grant.grant_id, agent_id="claude-code", cents=200)
    assert child_id
    assert broker.get(child_id).principal_chain == ("coder", "claude-code")
    assert broker.get(grant.grant_id).available_cents == 300


def test_a_sub_grant_cannot_exceed_the_task(broker):
    grant = broker.root("coder", task_id="t1", capabilities={WRITE, SHELL},
                        budget_cents=100)
    assert delegate_budget(grant_id=grant.grant_id, agent_id="claude-code", cents=500) == ""


def test_delegating_without_governance_is_a_no_op():
    assert delegate_budget(grant_id="", agent_id="claude-code", cents=100) == ""


# ------------------------------------------------------------------- teardown

def test_finishing_a_task_revokes_what_it_issued(broker):
    """Authority outliving its task is standing privilege wearing a task's name."""
    grant = broker.root("coder", task_id="t1", capabilities={WRITE, SHELL},
                        budget_cents=500)
    child_id = delegate_budget(grant_id=grant.grant_id, agent_id="claude-code", cents=100)

    assert release_task("t1") == 2

    assert _authorize(grant.grant_id)[0] is False
    assert broker.get(child_id).revoked is True


def test_teardown_is_scoped_to_one_task(broker):
    a = broker.root("coder", task_id="t1", capabilities={WRITE, SHELL}, budget_cents=100)
    b = broker.root("coder", task_id="t2", capabilities={WRITE, SHELL}, budget_cents=100)
    release_task("t1")
    assert _authorize(a.grant_id)[0] is False
    assert _authorize(b.grant_id)[0] is True


def test_teardown_without_governance_is_harmless():
    assert release_task("t1") == 0


# ------------------------------------------------- the engine issues the grant

def test_the_engine_puts_a_grant_on_the_task_context():
    """
    End to end: the worker reads `delegation_grant_id` from its context, so the engine
    has to put it there or every handoff is ungoverned.
    """
    from sovereign_os.governance.engine import GovernanceEngine
    from sovereign_os.ledger.unified_ledger import UnifiedLedger
    from sovereign_os.models.charter import load_charter
    from sovereign_os.governance.strategist import PlannedTask

    auth = SovereignAuth()
    auth._set_score("worker-1", 95)
    broker = DelegationBroker(eligibility=auth.check_permission_for)
    engine = GovernanceEngine(load_charter("charter.default.yaml"), UnifiedLedger(),
                              auth=auth, delegation_broker=broker)

    task = PlannedTask(task_id="t1", description="d", required_skill="code",
                       estimated_token_budget=5000)
    engine._task_estimate_cents["t1"] = 250
    grant_id = engine._issue_task_grant(task, "worker-1")

    assert grant_id
    grant = broker.get(grant_id)
    assert grant.task_id == "t1"
    assert grant.budget_cents == 250, "the sub-agent spends the CFO's approved ceiling"
    assert Capability.WRITE_FILES in grant.capabilities    # 'code' needs to write


def test_no_broker_means_no_grant_and_no_crash():
    from sovereign_os.governance.engine import GovernanceEngine
    from sovereign_os.ledger.unified_ledger import UnifiedLedger
    from sovereign_os.models.charter import load_charter
    from sovereign_os.governance.strategist import PlannedTask

    engine = GovernanceEngine(load_charter("charter.default.yaml"), UnifiedLedger())
    task = PlannedTask(task_id="t1", description="d", required_skill="code")
    assert engine._issue_task_grant(task, "worker-1") == ""
