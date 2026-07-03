"""
Tests for governance Prometheus metrics: audit rubric histograms, circuit-breaker
gauges/trip counter, JIT lease + agent-trust gauges, and /metrics wiring.
"""

import pytest

from sovereign_os.telemetry import tracer


def _skip_if_no_prometheus():
    if not tracer._PROMETHEUS_AVAILABLE:
        pytest.skip("prometheus_client not installed")


def test_trip_reason_kind_buckets():
    assert tracer._trip_reason_kind("session spend 500 reached ceiling 500") == "ceiling"
    assert tracer._trip_reason_kind("3 consecutive audit failures") == "failure_streak"
    assert tracer._trip_reason_kind("ROI 0.09 below floor 1.00") == "roi"
    assert tracer._trip_reason_kind("something else") == "other"


def test_record_audit_rubric_exports_scores():
    _skip_if_no_prometheus()
    tracer.record_audit_rubric("coding", 0.88, {"correctness": 0.9, "robustness": 0.8})
    out = tracer.get_prometheus_metrics_output().decode()
    assert 'sovereign_audit_score_count{category="coding"}' in out
    assert 'sovereign_audit_criterion_score_count{category="coding",criterion="correctness"}' in out


def test_record_breaker_trip_counter():
    _skip_if_no_prometheus()
    tracer.record_breaker_trip("session spend reached ceiling")
    out = tracer.get_prometheus_metrics_output().decode()
    assert 'sovereign_breaker_trips_total{reason="ceiling"}' in out


def test_set_governance_gauges_reflects_state():
    _skip_if_no_prometheus()
    tracer.set_governance_gauges(
        breaker_status={"spent_cents": 320, "revenue_cents": 640, "session_ceiling_cents": 800,
                        "consecutive_failures": 2, "roi": 2.0, "tripped": True},
        active_leases=3,
        agent_trust={"agent-x": {"trust_score": 77}},
    )
    out = tracer.get_prometheus_metrics_output().decode()
    assert "sovereign_breaker_session_spend_cents 320" in out
    assert "sovereign_breaker_tripped 1" in out
    assert "sovereign_active_leases 3" in out
    assert 'sovereign_agent_trust_score{agent="agent-x"} 77' in out


def test_roi_none_maps_to_minus_one():
    _skip_if_no_prometheus()
    tracer.set_governance_gauges(breaker_status={"spent_cents": 0, "roi": None, "tripped": False})
    out = tracer.get_prometheus_metrics_output().decode()
    assert "sovereign_breaker_roi -1" in out


def test_recorders_are_noop_without_client(monkeypatch):
    # Simulate prometheus_client missing: recorders must not raise.
    monkeypatch.setattr(tracer, "_audit_score", None)
    monkeypatch.setattr(tracer, "_breaker_trips_total", None)
    monkeypatch.setattr(tracer, "_breaker_spend_cents", None)
    tracer.record_audit_rubric("coding", 0.9, {"correctness": 0.9})
    tracer.record_breaker_trip("ceiling")
    tracer.set_governance_gauges(breaker_status={"spent_cents": 1, "roi": None})  # no raise


@pytest.mark.asyncio
async def test_metrics_endpoint_exposes_governance_gauges():
    _skip_if_no_prometheus()
    from fastapi.testclient import TestClient

    from sovereign_os.agents.auth import Capability, SovereignAuth
    from sovereign_os.governance.circuit_breaker import SpendCircuitBreaker
    from sovereign_os.governance.engine import GovernanceEngine
    from sovereign_os.ledger.unified_ledger import UnifiedLedger
    from sovereign_os.models.charter import Charter
    from sovereign_os.web.app import create_app

    led = UnifiedLedger(); led.record_usd(1000)
    auth = SovereignAuth(base_trust_score=90)
    auth.grant_lease("coder-m", Capability.EXECUTE_SHELL, task_id="t1", ttl_seconds=60)
    br = SpendCircuitBreaker(session_ceiling_cents=500); br.record_spend(250)
    engine = GovernanceEngine(Charter(mission="m"), led, auth=auth, circuit_breaker=br)
    client = TestClient(create_app(engine=engine, ledger=led, auth=auth))
    out = client.get("/metrics").text
    assert "sovereign_breaker_session_spend_cents 250" in out
    assert "sovereign_active_leases 1" in out
    assert "sovereign_breaker_session_ceiling_cents 500" in out
