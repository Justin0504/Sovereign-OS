"""Tests for the dashboard governance-guardrails API: circuit breaker + JIT leases."""

import os

import pytest

from sovereign_os.agents.auth import Capability, SovereignAuth
from sovereign_os.governance.circuit_breaker import SpendCircuitBreaker
from sovereign_os.governance.engine import GovernanceEngine
from sovereign_os.ledger.unified_ledger import UnifiedLedger
from sovereign_os.models.charter import Charter
from sovereign_os.web.app import create_app


@pytest.fixture
def client():
    led = UnifiedLedger()
    led.record_usd(1000)
    auth = SovereignAuth(base_trust_score=90)
    auth.record_audit("coder-1", passed=True, score=0.9, category="coding")
    auth.grant_lease("coder-1", Capability.EXECUTE_SHELL, task_id="task-1", ttl_seconds=120, max_uses=3)
    breaker = SpendCircuitBreaker(session_ceiling_cents=500, max_consecutive_failures=3)
    breaker.record_spend(300)
    breaker.record_revenue(900)
    engine = GovernanceEngine(Charter(mission="m"), led, auth=auth, circuit_breaker=breaker)
    app = create_app(engine=engine, ledger=led, auth=auth)
    try:
        from fastapi.testclient import TestClient
        return TestClient(app)
    except (ImportError, AttributeError) as e:
        if os.environ.get("GITHUB_ACTIONS"):
            raise RuntimeError(f"TestClient required in CI: {e}") from e
        pytest.skip(f"TestClient not available: {e}")


def test_governance_endpoint_reports_breaker_leases_and_agents(client):
    d = client.get("/api/governance").json()
    b = d["breaker"]
    assert b["spent_cents"] == 300 and b["revenue_cents"] == 900 and b["roi"] == 3.0
    assert b["session_ceiling_cents"] == 500 and b["tripped"] is False
    assert d["breaker_configured"] is True
    # one active JIT lease
    assert len(d["leases"]) == 1
    lease = d["leases"][0]
    assert lease["agent_id"] == "coder-1" and lease["capability"] == "execute_shell"
    assert lease["task_id"] == "task-1" and lease["max_uses"] == 3
    # agent trust snapshot present
    assert "coder-1" in d["agents"]
    assert d["agents"]["coder-1"]["trust_score"] >= 90


def test_governance_reset_clears_session(client):
    assert client.get("/api/governance").json()["breaker"]["spent_cents"] == 300
    r = client.post("/api/governance/reset").json()
    assert r["ok"] is True and r["breaker"]["spent_cents"] == 0
    assert client.get("/api/governance").json()["breaker"]["spent_cents"] == 0


def test_dashboard_html_has_guardrails_panel(client):
    html = client.get("/").text
    assert "panel-guardrails" in html and "fetchGuardrails" in html


def test_dashboard_html_has_quality_scorecard(client):
    html = client.get("/").text
    assert "renderScorecard" in html and "Quality scorecard" in html
    assert "sc-bars" in html  # per-criterion rubric bars


def test_job_result_passes_through_rubric_sub_scores(client):
    """The result API surfaces per-category rubric sub_scores + category for the scorecard."""
    import sovereign_os.web.app as m

    m._job_results[4242] = {
        "goal": "Fix the parser bug",
        "tasks": [{"task_id": "task-1-code_assistant", "skill": "code_assistant",
                   "output": "patched", "success": True}],
        "combined_output": "patched",
        "audit_reports": [{
            "task_id": "task-1-code_assistant", "passed": True, "score": 0.86,
            "reason": "solid fix", "suggested_fix": "", "category": "coding",
            "kpi_name": "default",
            "sub_scores": {"correctness": 0.9, "completeness": 0.85,
                           "robustness": 0.8, "relevance": 0.9, "safety": 1.0},
        }],
    }
    d = client.get("/api/jobs/4242/result").json()
    rep = d["audit_reports"][0]
    assert rep["category"] == "coding"
    assert rep["sub_scores"]["robustness"] == 0.8
    assert set(rep["sub_scores"]) == {"correctness", "completeness", "robustness", "relevance", "safety"}
