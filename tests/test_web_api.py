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
    """TestClient for app. Skips if httpx incompatible."""
    try:
        from fastapi.testclient import TestClient
        return TestClient(app)
    except (ImportError, AttributeError) as e:
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
