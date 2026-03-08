"""Integration tests for Job queue and Web API."""

import pytest

try:
    from fastapi.testclient import TestClient
except Exception as e:
    pytest.skip(
        f"FastAPI TestClient/httpx not available or incompatible: {e}",
        allow_module_level=True,
    )

from sovereign_os.web.app import create_app


@pytest.fixture
def app():
    """App with no engine (jobs/tasks still testable)."""
    return create_app(engine=None, ledger=None, auth=None, charter_name="Test")


@pytest.fixture
def client(app):
    return TestClient(app)


def test_get_status_without_engine(client):
    r = client.get("/api/status")
    assert r.status_code == 200
    data = r.json()
    assert "balance" in data
    assert data["charter"] == "Test"


def test_get_tasks_empty(client):
    r = client.get("/api/tasks")
    assert r.status_code == 200
    assert r.json()["tasks"] == []


def test_post_job_creates_pending(client):
    import os
    from unittest.mock import patch
    with patch.dict(os.environ, {"SOVEREIGN_AUTO_APPROVE_JOBS": "false"}, clear=False):
        r = client.post(
            "/api/jobs",
            json={"goal": "Do something", "charter": "Test", "amount_cents": 100},
        )
    assert r.status_code == 200
    job = r.json()["job"]
    assert job["status"] == "pending"
    assert job["goal"] == "Do something"
    assert job["amount_cents"] == 100


def test_get_jobs_list(client):
    client.post("/api/jobs", json={"goal": "G1", "charter": "Test"})
    r = client.get("/api/jobs")
    assert r.status_code == 200
    jobs = r.json()["jobs"]
    assert len(jobs) >= 1
    # API returns most recent first (sorted by -job_id)
    assert jobs[0]["goal"] == "G1"


def test_approve_job(client):
    r = client.post("/api/jobs", json={"goal": "G2", "charter": "Test"})
    job_id = r.json()["job"]["job_id"]
    r2 = client.post(f"/api/jobs/{job_id}/approve")
    assert r2.status_code == 200
    assert r2.json()["job"]["status"] == "approved"


def test_run_returns_503_without_engine(client):
    r = client.post("/api/run", json={"goal": "Run me"})
    assert r.status_code == 503
    assert "Engine not configured" in r.json().get("error", "")
