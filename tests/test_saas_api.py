"""Tests for the multi-tenant SaaS API (signup, auth, config, limits, console)."""

import tempfile

import pytest

from sovereign_os.saas.api import create_saas_app
from sovereign_os.saas.tenancy import TenantStore


@pytest.fixture
def client():
    store = TenantStore(tempfile.mkdtemp())
    try:
        from fastapi.testclient import TestClient
        return TestClient(create_saas_app(store))
    except Exception as e:  # pragma: no cover
        pytest.skip(f"TestClient unavailable: {e}")


def _signup(client, name="Acme", plan="pro"):
    r = client.post("/saas/tenants", json={"name": name, "plan": plan})
    return r


def test_health_and_plans(client):
    assert client.get("/saas/health").json()["status"] == "ok"
    names = {p["name"] for p in client.get("/saas/plans").json()["plans"]}
    assert {"free", "pro", "team"} <= names


def test_signup_returns_key_once_no_leak(client):
    r = _signup(client)
    assert r.status_code == 200
    d = r.json()
    assert d["api_key"].startswith("sk_ten_")
    assert "api_key" not in d["tenant"]  # tenant object never carries the key


def test_signup_requires_name(client):
    assert client.post("/saas/tenants", json={"name": ""}).status_code == 400


def test_auth_required(client):
    key = _signup(client).json()["api_key"]
    assert client.get("/saas/tenants/me").status_code == 401
    assert client.get("/saas/tenants/me", headers={"X-Tenant-Key": "bad"}).status_code == 401
    assert client.get("/saas/tenants/me", headers={"X-Tenant-Key": key}).status_code == 200


def test_config_masks_and_persists(client):
    key = _signup(client).json()["api_key"]
    H = {"X-Tenant-Key": key}
    r = client.put("/saas/tenants/me/config", headers=H,
                   json={"anthropic_api_key": "sk-ant-supersecretkey123", "x402_pay_to": "0xabc"})
    cfg = r.json()["tenant"]["config"]
    assert "supersecret" not in str(cfg) and cfg["anthropic_api_key"].endswith("123")
    assert cfg["x402_pay_to"] == "0xabc"
    # persists across a fresh read
    cfg2 = client.get("/saas/tenants/me", headers=H).json()["tenant"]["config"]
    assert cfg2["anthropic_api_key"].endswith("123")


def test_mission_validation_and_limits(client):
    key = _signup(client).json()["api_key"]
    H = {"X-Tenant-Key": key}
    # no goal -> 400
    assert client.post("/saas/tenants/me/missions", headers=H, json={}).status_code == 400
    # no LLM key configured yet -> 402 (limit/precondition)
    r = client.post("/saas/tenants/me/missions", headers=H, json={"goal": "Do a thing"})
    assert r.status_code == 402 and "key" in r.json()["detail"]


def test_console_served(client):
    html = client.get("/").text
    assert "Sovereign-OS Console" in html and "Create workspace" in html


def test_tenants_are_isolated(client):
    a = _signup(client, "A").json()["api_key"]
    b = _signup(client, "B").json()["api_key"]
    ma = client.get("/saas/tenants/me", headers={"X-Tenant-Key": a}).json()["tenant"]
    mb = client.get("/saas/tenants/me", headers={"X-Tenant-Key": b}).json()["tenant"]
    assert ma["id"] != mb["id"] and ma["name"] == "A" and mb["name"] == "B"
