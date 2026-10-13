"""
Tests for the browser-facing posture of the hosted console.

The deployed service was returning no security headers at all — no CSP, no HSTS, no
nosniff, no frame-ancestors — while serving a page that stores a tenant API key in
localStorage and built its DOM with `innerHTML` from the workspace name the tenant chose
at signup. Each of those alone is survivable; together they are an exfiltration path, and
the gap was invisible because every functional test passed.

These drive the real endpoints and assert on real response headers. A header set by a
middleware that stops being installed looks exactly like one that works, so nothing here
calls the middleware directly.
"""

import re
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


@pytest.fixture
def hosted_client(monkeypatch):
    # hosted=True refuses to start without a deployment secret (tenant provider keys
    # would otherwise be written in plaintext). That guard is tested elsewhere; here it
    # just has to be satisfied so the HSTS assertion actually runs rather than skipping.
    monkeypatch.setenv("SOVEREIGN_SAAS_SECRET", "test-secret-not-a-real-deployment-key")
    store = TenantStore(tempfile.mkdtemp())
    try:
        from fastapi.testclient import TestClient
        return TestClient(create_saas_app(store, hosted=True))
    except Exception as e:  # pragma: no cover
        pytest.skip(f"TestClient unavailable: {e}")


# --------------------------------------------------------------- headers are present

@pytest.mark.parametrize("path", ["/", "/saas/health", "/saas/plans"])
def test_every_response_carries_the_browser_guards(client, path):
    h = client.get(path).headers
    assert h["X-Content-Type-Options"] == "nosniff"
    assert h["X-Frame-Options"] == "DENY"
    assert h["Referrer-Policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in h["Content-Security-Policy"]


def test_an_error_response_is_still_guarded(client):
    """401s are responses too, and a JSON error body sniffed as HTML is the usual trick."""
    r = client.get("/saas/tenants/me")
    assert r.status_code == 401
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert "Content-Security-Policy" in r.headers


def test_hsts_only_where_it_is_not_a_lie(client, hosted_client):
    """Advertising HSTS over plain HTTP in local dev would be false, so it is not sent."""
    assert "Strict-Transport-Security" in hosted_client.get("/").headers
    assert "Strict-Transport-Security" not in client.get("/").headers


# ------------------------------------------------------------------ the CSP is usable

def _nonce_of(resp):
    return re.search(r"script-src 'nonce-([^']+)'", resp.headers["Content-Security-Policy"]).group(1)


def test_the_console_script_carries_the_nonce_from_its_own_response(client):
    """
    A nonce in the header that the page does not use means a blank console, which is how
    a CSP normally gets reverted to 'unsafe-inline'.
    """
    r = client.get("/")
    nonce = _nonce_of(r)
    assert f'<script nonce={nonce}>' in r.text
    assert f'<style nonce={nonce}>' in r.text
    assert "__CSP_NONCE__" not in r.text, "placeholder was not substituted"


def test_the_nonce_is_fresh_per_response(client):
    assert _nonce_of(client.get("/")) != _nonce_of(client.get("/"))


def test_the_console_has_no_inline_handlers_left(client):
    """
    A nonce whitelists a <script> block and never an `onclick=` attribute, so one leftover
    handler forces 'unsafe-inline' back into the policy — which whitelists the injected
    script too, making the whole CSP decorative.
    """
    assert "onclick=" not in client.get("/").text


def test_the_policy_denies_by_default(client):
    csp = client.get("/").headers["Content-Security-Policy"]
    assert csp.startswith("default-src 'none'")
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp


# ------------------------------------------------- the DOM sink next to the stored key

def test_the_console_does_not_build_dom_from_strings(client):
    """
    The workspace name is tenant-controlled and the tenant API key is in localStorage on
    the same origin. `textContent` is what keeps those two facts from composing.
    """
    body = client.get("/").text
    code = body.split("<script", 1)[1]
    assert ".innerHTML" not in code
    assert 'localStorage.getItem("sov_key")' in code, "key storage moved; re-check this test"


# ------------------------------------------------------- bounded input at the boundary

def test_a_workspace_name_is_length_bounded(client):
    r = client.post("/saas/tenants", json={"name": "A" * 5000, "plan": "free"})
    assert r.status_code == 400
    assert "80 characters" in r.json()["detail"]


def test_a_workspace_name_cannot_carry_control_characters(client):
    r = client.post("/saas/tenants", json={"name": "Acme\n2026-01-01 INFO forged", "plan": "free"})
    assert r.status_code == 400


def test_an_ordinary_name_still_works(client):
    """The guard has to not break the product it guards."""
    r = client.post("/saas/tenants", json={"name": "Acme Robotics — 北京", "plan": "free"})
    assert r.status_code == 200
    assert r.json()["tenant"]["name"] == "Acme Robotics — 北京"


def test_a_name_that_is_markup_is_stored_verbatim_and_never_executed(client):
    """
    Angle brackets are legal in a name; the defence is the rendering, not a blocklist.
    This pins that we did not quietly start stripping characters instead.
    """
    name = '<img src=x onerror=alert(1)>'
    r = client.post("/saas/tenants", json={"name": name, "plan": "free"})
    assert r.status_code == 200
    assert r.json()["tenant"]["name"] == name
    assert name not in client.get("/").text
