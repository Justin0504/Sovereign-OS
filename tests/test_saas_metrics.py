"""
The hosted control plane's operational metrics.

The self-host dashboard exposes /metrics; the hosted service had none, so an operator had
no view of signup rate, mission throughput, or the auth-failure rate that signals an
attack. This endpoint adds that, under two constraints that come from its being
multi-tenant:

  * aggregate only -- no series is labelled by tenant, or a scraper could enumerate
    tenants and their activity through the monitoring port;
  * off by default -- disabled unless SOVEREIGN_METRICS_TOKEN is set, then bearer-token
    protected, because even aggregates are business-sensitive and an open metrics endpoint
    is an information-disclosure surface.

These drive the real endpoints: the counters move because real signups, missions and
failed auths happened, not because a counter function was called directly.
"""

import tempfile

import pytest

from sovereign_os.saas import platform_metrics
from sovereign_os.saas.api import create_saas_app
from sovereign_os.saas.tenancy import TenantStore

TOKEN = "op-metrics-token-not-a-secret"


@pytest.fixture(autouse=True)
def clean_counters():
    platform_metrics.reset()
    yield
    platform_metrics.reset()


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("SOVEREIGN_METRICS_TOKEN", TOKEN)
    try:
        from fastapi.testclient import TestClient
        return TestClient(create_saas_app(TenantStore(tempfile.mkdtemp())))
    except Exception as e:  # pragma: no cover
        pytest.skip(f"TestClient unavailable: {e}")


def _scrape(client):
    return client.get("/saas/metrics", headers={"Authorization": f"Bearer {TOKEN}"})


def _value(text, name):
    for line in text.splitlines():
        if line.startswith(name + " "):
            return float(line.split()[1])
    raise AssertionError(f"{name} not in metrics output")


# --------------------------------------------------------------- access control

def test_metrics_are_disabled_without_a_token(monkeypatch):
    monkeypatch.delenv("SOVEREIGN_METRICS_TOKEN", raising=False)
    from fastapi.testclient import TestClient
    c = TestClient(create_saas_app(TenantStore(tempfile.mkdtemp())))
    assert c.get("/saas/metrics").status_code == 404


def test_metrics_require_the_bearer_token(client):
    assert client.get("/saas/metrics").status_code == 401
    assert client.get("/saas/metrics",
                      headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_the_right_token_scrapes(client):
    r = _scrape(client)
    assert r.status_code == 200
    assert "text/plain" in r.headers["content-type"]
    assert "sovereign_missions_total" in r.text


# ------------------------------------------------------ counters reflect real events

def test_a_signup_increments_the_signup_counter(client):
    before = _value(_scrape(client).text, "sovereign_signups_total")
    client.post("/saas/tenants", json={"name": "Acme", "plan": "free"})
    assert _value(_scrape(client).text, "sovereign_signups_total") == before + 1


def test_a_bad_tenant_key_increments_the_auth_failure_counter(client):
    before = _value(_scrape(client).text, "sovereign_auth_failures_total")
    client.get("/saas/tenants/me", headers={"X-Tenant-Key": "sk_ten_nope"})
    assert _value(_scrape(client).text, "sovereign_auth_failures_total") == before + 1


def test_a_demo_run_increments_the_demo_counter(client):
    before = _value(_scrape(client).text, "sovereign_demo_runs_total")
    client.get("/saas/demo")
    assert _value(_scrape(client).text, "sovereign_demo_runs_total") == before + 1


def test_a_refused_config_increments_the_rejected_counter(client):
    key = client.post("/saas/tenants", json={"name": "A", "plan": "free"}).json()["api_key"]
    before = _value(_scrape(client).text, "sovereign_config_rejected_total")
    client.put("/saas/tenants/me/config", json={"plan": "does-not-exist"},
               headers={"X-Tenant-Key": key})
    assert _value(_scrape(client).text, "sovereign_config_rejected_total") == before + 1


def test_the_tenant_gauge_counts_live_workspaces(client):
    client.post("/saas/tenants", json={"name": "One", "plan": "free"})
    client.post("/saas/tenants", json={"name": "Two", "plan": "free"})
    assert _value(_scrape(client).text, "sovereign_tenants") == 2


# -------------------------------------------------- the cross-tenant leak guard

def test_no_metric_is_labelled_by_tenant(client):
    """
    The constraint that makes this safe to expose at all. A series a scraper could break
    down by tenant would leak one tenant's activity to anyone with the token.
    """
    c1 = client.post("/saas/tenants", json={"name": "Acme Robotics", "plan": "free"}).json()
    client.get("/saas/demo")
    text = _scrape(client).text
    assert c1["tenant"]["id"] not in text, "a tenant id appears in the metrics output"
    assert "Acme Robotics" not in text, "a tenant name appears in the metrics output"
    # No Prometheus label braces at all -- every series is a bare total.
    assert "{" not in text, "a labelled series could carry per-tenant breakdown"


def test_the_token_is_compared_in_constant_time(client):
    """A metrics token is a credential; the check uses hmac.compare_digest, not ==."""
    import inspect

    from sovereign_os.saas import api

    src = inspect.getsource(api.create_saas_app)
    assert "compare_digest" in src, "the metrics token must be compared in constant time"
