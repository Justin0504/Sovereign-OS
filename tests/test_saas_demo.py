"""
The public demo: a governed run anyone can watch without an API key.

The console asked a visitor for an Anthropic key before showing anything, so anyone
evaluating the system -- the reason a console is public at all -- saw a signup form and
left. Governance does not need a model, so the gates below are the real ones.

The risk this file guards is overstatement. A demo that implies the agent did the work,
or that quietly stops exercising the real components, is worse than no demo.
"""

import tempfile

import pytest

from sovereign_os.saas.api import create_saas_app
from sovereign_os.saas.demo import run_demo_mission
from sovereign_os.saas.tenancy import TenantStore


@pytest.fixture
def client():
    try:
        from fastapi.testclient import TestClient
        return TestClient(create_saas_app(TenantStore(tempfile.mkdtemp())))
    except Exception as e:  # pragma: no cover
        pytest.skip(f"TestClient unavailable: {e}")


def test_the_demo_needs_no_credentials(client):
    """The entire point. If this ever needs a header, the front door is shut again."""
    assert client.get("/saas/demo").status_code == 200


def test_the_trace_contains_refusals():
    """
    A demo where everything is approved demonstrates nothing: the product's claim is
    that it says no, so the trace has to contain a no.
    """
    d = run_demo_mission()
    assert d["refusals"] >= 2
    decisions = {s["decision"] for s in d["steps"]}
    assert "refused" in decisions and "allowed" in decisions


def test_the_budget_refusal_is_the_real_treasury():
    """Not a string: the CFO refused it against the charter's per-task ceiling."""
    d = run_demo_mission()
    refusal = next(s for s in d["steps"]
                   if s["stage"] == "budget" and s["decision"] == "refused")
    assert refusal["detail"]["estimated_cents"] > refusal["detail"]["per_task_ceiling_cents"]
    assert "ceiling" in refusal["detail"]["reason"]


def test_the_permission_refusal_cites_a_real_threshold():
    d = run_demo_mission()
    refusal = next(s for s in d["steps"]
                   if s["stage"] == "permission" and s["decision"] == "refused")
    assert refusal["detail"]["trust_score"] < refusal["detail"]["threshold"]


def test_every_audited_task_carries_a_real_proof_hash():
    """
    SHA-256 is 64 hex characters. A placeholder here would make the most load-bearing
    claim on the page a decoration.
    """
    d = run_demo_mission()
    audits = [s for s in d["steps"] if s["stage"] == "audit"]
    assert audits, "the audit step vanished from the trace"
    for a in audits:
        h = a["detail"]["proof_hash"]
        assert len(h) == 64 and all(c in "0123456789abcdef" for c in h)


def test_distinct_tasks_get_distinct_hashes():
    d = run_demo_mission()
    hashes = [s["detail"]["proof_hash"] for s in d["steps"] if s["stage"] == "audit"]
    assert len(set(hashes)) == len(hashes)


def test_the_ledger_total_matches_what_was_approved():
    """The arithmetic has to be the system's, or the trace is theatre."""
    d = run_demo_mission()
    approved = sum(s["detail"]["estimated_cents"] for s in d["steps"]
                   if s["stage"] == "budget" and s["decision"] == "allowed")
    ledger = next(s for s in d["steps"] if s["stage"] == "ledger")
    assert ledger["detail"]["token_cents"] == approved


def test_the_refused_task_never_reaches_the_ledger():
    d = run_demo_mission()
    refused = {s["detail"]["task_id"] for s in d["steps"]
               if s["stage"] == "budget" and s["decision"] == "refused"}
    audited = {s["detail"]["task_id"] for s in d["steps"] if s["stage"] == "audit"}
    assert refused and not (refused & audited), \
        "a task the CFO refused was executed and audited anyway"


def test_the_response_says_plainly_that_no_model_ran():
    """Honesty is load-bearing here: a visitor must not read this as finished work."""
    d = run_demo_mission()
    assert d["demo"] is True
    text = d["disclaimer"].lower()
    assert "no model" in text and "placeholder" in text


def test_the_demo_is_rate_limited(client):
    """Unauthenticated and it runs real work, so it is worth hammering."""
    codes = {client.get("/saas/demo").status_code for _ in range(40)}
    assert 429 in codes, "an unauthenticated endpoint with no limit is an invitation"
