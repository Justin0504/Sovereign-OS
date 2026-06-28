"""Tests for Web API: job validation (400), rate limit (429)."""

import os
from unittest.mock import patch

import pytest

from sovereign_os.ledger.unified_ledger import UnifiedLedger
from sovereign_os.web.app import create_app


@pytest.fixture
def app():
    """App with minimal engine/ledger so POST /api/jobs is available."""
    led = UnifiedLedger()
    led.record_usd(1000)
    return create_app(engine=None, ledger=led)


@pytest.fixture
def client(app):
    """TestClient for app. In CI (GITHUB_ACTIONS) fail if unavailable; locally skip."""
    try:
        from fastapi.testclient import TestClient
        return TestClient(app)
    except (ImportError, AttributeError) as e:
        if os.environ.get("GITHUB_ACTIONS"):
            raise RuntimeError(f"TestClient required in CI: {e}") from e
        pytest.skip(f"TestClient not available: {e}")


def test_jobs_create_rejects_goal_too_long(client):
    """POST /api/jobs with goal length > 20000 returns 400."""
    from sovereign_os.web.app import JOB_GOAL_MAX_LEN
    r = client.post(
        "/api/jobs",
        json={
            "goal": "x" * (JOB_GOAL_MAX_LEN + 1),
            "amount_cents": 100,
            "currency": "USD",
        },
    )
    assert r.status_code == 400
    assert "goal" in (r.json().get("detail") or "").lower()


def test_jobs_create_rejects_amount_cents_out_of_range(client):
    """POST /api/jobs with amount_cents < 0 or > 1000000 returns 400."""
    from sovereign_os.web.app import JOB_AMOUNT_CENTS_MAX, JOB_AMOUNT_CENTS_MIN
    r1 = client.post("/api/jobs", json={"goal": "Ok", "amount_cents": -1, "currency": "USD"})
    assert r1.status_code == 400
    r2 = client.post(
        "/api/jobs",
        json={"goal": "Ok", "amount_cents": JOB_AMOUNT_CENTS_MAX + 1, "currency": "USD"},
    )
    assert r2.status_code == 400
    assert "amount_cents" in (r2.json().get("detail") or "").lower()


def test_jobs_create_rejects_invalid_callback_url(client):
    """POST /api/jobs with invalid callback_url returns 400."""
    r = client.post(
        "/api/jobs",
        json={
            "goal": "Ok",
            "amount_cents": 0,
            "currency": "USD",
            "callback_url": "not-a-url",
        },
    )
    assert r.status_code == 400
    assert "callback_url" in (r.json().get("detail") or "").lower()


def test_jobs_create_accepts_valid_callback_url(client):
    """POST /api/jobs with valid https callback_url returns 200."""
    r = client.post(
        "/api/jobs",
        json={
            "goal": "Summarize market.",
            "amount_cents": 0,
            "currency": "USD",
            "callback_url": "https://example.com/hook",
        },
    )
    assert r.status_code == 200
    assert "job" in r.json()


def test_cost_summary_endpoint():
    """GET /api/cost_summary returns per-model and per-agent cost breakdowns from the ledger."""
    led = UnifiedLedger()
    led.record_usd(1000)
    led.record_token("gpt-4o", 1000, 500, agent_id="research", task_id="t1", estimated_usd_cents=5)
    led.record_token("gpt-4o-mini", 2000, 1000, agent_id="writer", task_id="t2", estimated_usd_cents=1)
    app = create_app(engine=None, ledger=led)
    try:
        from fastapi.testclient import TestClient
    except (ImportError, AttributeError) as e:
        if os.environ.get("GITHUB_ACTIONS"):
            raise RuntimeError(f"TestClient required in CI: {e}") from e
        pytest.skip(f"TestClient not available: {e}")
    r = TestClient(app).get("/api/cost_summary")
    assert r.status_code == 200
    data = r.json()
    assert data["token_cost_cents"] == 6
    assert data["total_tokens"] == 4500
    by_model = {row["key"]: row["cost_cents"] for row in data["by_model"]}
    assert by_model == {"gpt-4o": 5, "gpt-4o-mini": 1}
    # Sorted by cost descending.
    assert data["by_model"][0]["key"] == "gpt-4o"
    by_agent = {row["key"]: row["cost_cents"] for row in data["by_agent"]}
    assert by_agent == {"research": 5, "writer": 1}
    # Budget-utilization fields present (no engine -> daily cap 0).
    assert "daily_spend_cents" in data
    assert "daily_cap_cents" in data


@patch.dict(os.environ, {"SOVEREIGN_JOB_RATE_LIMIT_PER_MIN": "2"}, clear=False)
def test_jobs_create_rate_limit_returns_429(client):
    """When rate limit is 2/min, third request from same client returns 429."""
    from sovereign_os.web.app import _job_rate_limit_times
    _job_rate_limit_times.clear()
    r1 = client.post("/api/jobs", json={"goal": "A", "amount_cents": 0, "currency": "USD"})
    r2 = client.post("/api/jobs", json={"goal": "B", "amount_cents": 0, "currency": "USD"})
    r3 = client.post("/api/jobs", json={"goal": "C", "amount_cents": 0, "currency": "USD"})
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r3.status_code == 429
    assert "rate limit" in (r3.json().get("detail") or "").lower()


def test_jobs_list_returns_limit_and_total(client):
    """GET /api/jobs?limit=N returns at most N jobs and a total count."""
    r = client.get("/api/jobs?limit=2")
    assert r.status_code == 200
    data = r.json()
    assert "jobs" in data
    assert "total" in data
    assert len(data["jobs"]) <= 2
    assert data["total"] >= len(data["jobs"])


def _app_with_engine():
    from sovereign_os.governance.engine import GovernanceEngine
    from sovereign_os.auditor import ReviewEngine
    from sovereign_os.auditor.review_engine import StubAuditor
    from sovereign_os.agents.auth import SovereignAuth
    from sovereign_os.models.charter import Charter, FiscalBoundaries
    led = UnifiedLedger(); led.record_usd(20000)
    charter = Charter(mission="m", fiscal_boundaries=FiscalBoundaries(max_task_cost_usd=50.0))
    eng = GovernanceEngine(charter, led, auth=SovereignAuth(),
                           review_engine=ReviewEngine(charter, judge=StubAuditor()))
    return create_app(engine=eng, ledger=led)


def _tc(app):
    try:
        from fastapi.testclient import TestClient
        return TestClient(app)
    except (ImportError, AttributeError) as e:
        if os.environ.get("GITHUB_ACTIONS"):
            raise RuntimeError(f"TestClient required in CI: {e}") from e
        pytest.skip(f"TestClient not available: {e}")


def test_oversight_hire_budget_gate_and_panel():
    import sovereign_os.web.app as appmod
    appmod._oversight_registry = None  # reset shared registry
    c = _tc(_app_with_engine())
    # Over the $50 ceiling -> budget gate rejects, nothing posted.
    over = c.post("/api/oversight/hire", json={"title": "Pricey", "price_cents": 8000}).json()
    assert over["posted"] is False
    # Affordable -> posted + funded (dry-run).
    ok = c.post("/api/oversight/hire", json={"title": "Cheap gig", "price_cents": 2000}).json()
    assert ok["posted"] is True and ok["escrow_id"]
    # Panel reflects it.
    panel = c.get("/api/oversight").json()
    assert panel["summary"].get("funded") == 1
    assert any(e["title"] == "Cheap gig" for e in panel["escrows"])


def test_oversight_poll_settles_via_quality_gate():
    import sovereign_os.web.app as appmod
    appmod._oversight_registry = None
    c = _tc(_app_with_engine())
    c.post("/api/oversight/hire", json={"title": "Settle me", "price_cents": 2000})
    settled = c.post("/api/oversight/poll").json()["settled"]
    assert len(settled) == 1 and settled[0]["action"] == "released"
    assert c.get("/api/oversight").json()["summary"].get("released") == 1
