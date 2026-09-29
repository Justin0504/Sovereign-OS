"""
Tests for the pre-flight cost forecast endpoint.

The design point being pinned: the band is reported as *absent* until enough tasks have
settled to have measured a spread. A confidence interval invented from two samples reads
as precision the system does not have, which is worse than admitting there is none yet.
"""

import pytest

from sovereign_os.governance.cost_model import record_cost, reset_cost


@pytest.fixture(autouse=True)
def clean_global():
    reset_cost()
    yield
    reset_cost()


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from sovereign_os.web.app import create_app

    return TestClient(create_app())


def _forecast(client, goal="Write a small function.", category="coding"):
    r = client.get("/api/cost/forecast", params={"goal": goal, "category": category})
    assert r.status_code == 200
    return r.json()


def test_forecast_returns_a_point_estimate(client):
    d = _forecast(client)
    assert d["point_cents"] > 0
    assert d["raw_cents"] > 0
    assert d["category"] == "coding"
    assert 0.5 <= d["complexity"] <= 2.0


def test_no_band_and_no_claimed_evidence_before_tasks_settle(client):
    d = _forecast(client)
    assert d["upper_cents"] is None
    assert d["calibration"]["has_evidence"] is False
    assert d["calibration"]["samples"] == 0
    assert d["calibration"]["factor"] == 1.0
    assert d["calibration"]["within_2x"] is None


def test_band_appears_once_there_is_a_measured_spread(client):
    for actual in (40, 90, 150, 220, 60, 300):
        record_cost("coding", 100, actual)
    d = _forecast(client)
    assert d["calibration"]["has_evidence"] is True
    assert d["calibration"]["samples"] == 6
    assert d["upper_cents"] is not None
    assert d["upper_cents"] >= d["point_cents"]


def test_a_learned_over_ask_lowers_the_point_estimate(client):
    before = _forecast(client)["point_cents"]
    for _ in range(40):
        record_cost("coding", 200, 50)        # work consistently costs a quarter of estimate
    after = _forecast(client)
    assert after["point_cents"] < before
    assert after["calibration"]["factor"] < 1.0


def test_calibration_quality_is_surfaced(client):
    for _ in range(10):
        record_cost("coding", 100, 110)       # tight and slightly over
    cal = _forecast(client)["calibration"]
    assert cal["within_2x"] == 1.0
    assert cal["overrun_rate"] == 1.0


def test_forecast_is_scoped_per_category(client):
    for _ in range(40):
        record_cost("coding", 200, 50)
    assert _forecast(client, category="writing")["calibration"]["samples"] == 0


def test_empty_goal_is_still_answerable(client):
    d = _forecast(client, goal="")
    assert d["point_cents"] > 0


def test_unknown_category_falls_back_without_erroring(client):
    d = _forecast(client, category="interpretive_dance")
    assert d["point_cents"] > 0
