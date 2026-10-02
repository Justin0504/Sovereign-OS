"""
Tests for rate limiting and the hosted startup preflight.

Both exist for the same reason: a publicly reachable endpoint that mints credentials and
spends model tokens is expensive to leave open, and a protection that is implemented but
never invoked protects nothing. The preflight tests are the second kind — `require_encryption`
had been written, tested, and called from nowhere.
"""

import tempfile

import pytest

from sovereign_os.saas.limits import AuthThrottle, RateLimiter
from sovereign_os.saas.secrets import SecretsNotConfigured
from sovereign_os.saas.tenancy import TenantStore

SECRET = "test-deployment-secret"


# --------------------------------------------------------------- token bucket

def test_a_burst_is_allowed_then_refused():
    now = [0.0]
    limiter = RateLimiter(capacity=3, refill_per_second=1, clock=lambda: now[0])
    assert [limiter.check("ip")[0] for _ in range(3)] == [True, True, True]
    allowed, retry_after = limiter.check("ip")
    assert allowed is False
    assert retry_after == pytest.approx(1.0)


def test_the_bucket_refills_over_time():
    now = [0.0]
    limiter = RateLimiter(capacity=2, refill_per_second=1, clock=lambda: now[0])
    limiter.check("ip"); limiter.check("ip")
    assert limiter.check("ip")[0] is False
    now[0] = 1.0
    assert limiter.check("ip")[0] is True


def test_a_refused_call_deducts_nothing():
    """A rejected caller must not dig itself deeper than an idle one."""
    now = [0.0]
    limiter = RateLimiter(capacity=1, refill_per_second=1, clock=lambda: now[0])
    limiter.check("ip")
    for _ in range(50):
        limiter.check("ip")
    now[0] = 1.0
    assert limiter.check("ip")[0] is True, "50 refusals must not extend the wait"


def test_refill_is_capped_at_capacity():
    now = [0.0]
    limiter = RateLimiter(capacity=2, refill_per_second=1, clock=lambda: now[0])
    now[0] = 10_000.0
    assert [limiter.check("ip")[0] for _ in range(3)] == [True, True, False]


def test_callers_are_limited_independently():
    limiter = RateLimiter(capacity=1, refill_per_second=1)
    assert limiter.check("a")[0] is True
    assert limiter.check("b")[0] is True
    assert limiter.check("a")[0] is False


def test_idle_buckets_are_prunable():
    """The key space is an attacker-controllable dict; it needs a bound."""
    now = [0.0]
    limiter = RateLimiter(capacity=2, refill_per_second=1, clock=lambda: now[0])
    for i in range(100):
        limiter.check(f"ip-{i}")
    assert limiter.tracked_keys == 100
    now[0] = 7200.0
    assert limiter.prune(max_idle_seconds=3600) == 100
    assert limiter.tracked_keys == 0


def test_a_bucket_still_in_debt_is_not_pruned():
    now = [0.0]
    limiter = RateLimiter(capacity=2, refill_per_second=0.0001, clock=lambda: now[0])
    limiter.check("ip"); limiter.check("ip")
    now[0] = 7200.0
    limiter.prune(max_idle_seconds=3600)
    assert limiter.tracked_keys == 1, "pruning a throttled caller would reset its limit"


def test_invalid_configuration_is_refused():
    with pytest.raises(ValueError):
        RateLimiter(capacity=0, refill_per_second=1)


# ------------------------------------------------------------- auth throttle

def test_lockout_after_repeated_failures():
    now = [0.0]
    throttle = AuthThrottle(max_failures=3, window_seconds=60, clock=lambda: now[0])
    for _ in range(3):
        assert throttle.is_locked("ip")[0] is False
        throttle.record_failure("ip")
    locked, retry_after = throttle.is_locked("ip")
    assert locked is True and retry_after == pytest.approx(60.0)


def test_the_window_expires():
    now = [0.0]
    throttle = AuthThrottle(max_failures=2, window_seconds=60, clock=lambda: now[0])
    throttle.record_failure("ip"); throttle.record_failure("ip")
    assert throttle.is_locked("ip")[0] is True
    now[0] = 61.0
    assert throttle.is_locked("ip")[0] is False


def test_a_success_clears_the_record():
    """A user who mistypes and then succeeds must not stay half-locked."""
    throttle = AuthThrottle(max_failures=3)
    throttle.record_failure("ip"); throttle.record_failure("ip")
    throttle.record_success("ip")
    throttle.record_failure("ip")
    assert throttle.is_locked("ip")[0] is False


# ------------------------------------------------------------- live endpoints

@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from sovereign_os.saas.api import create_saas_app

    return TestClient(create_saas_app(TenantStore(tempfile.mkdtemp())))


def test_signup_is_rate_limited(client):
    codes = [client.post("/saas/tenants", json={"name": f"t{i}"}).status_code
             for i in range(8)]
    assert 200 in codes
    assert 429 in codes, "an open signup endpoint that mints credentials needs a ceiling"
    assert codes.index(429) > codes.index(200)


def test_a_429_carries_retry_after(client):
    last = None
    for i in range(8):
        last = client.post("/saas/tenants", json={"name": f"t{i}"})
    assert last.status_code == 429
    assert int(last.headers["Retry-After"]) >= 1


def test_repeated_bad_keys_are_throttled(client):
    for _ in range(10):
        client.get("/saas/tenants/me", headers={"X-Tenant-Key": "sk_ten_wrong"})
    r = client.get("/saas/tenants/me", headers={"X-Tenant-Key": "sk_ten_wrong"})
    assert r.status_code == 429


def test_a_valid_key_is_not_blocked_by_someone_elses_failures(client):
    key = client.post("/saas/tenants", json={"name": "Acme"}).json()["api_key"]
    for _ in range(3):
        client.get("/saas/tenants/me", headers={"X-Tenant-Key": "sk_ten_wrong"})
    # Below the lockout threshold, and the success clears the record.
    assert client.get("/saas/tenants/me", headers={"X-Tenant-Key": key}).status_code == 200
    for _ in range(12):
        client.get("/saas/tenants/me", headers={"X-Tenant-Key": "sk_ten_wrong"})
    assert client.get("/saas/tenants/me", headers={"X-Tenant-Key": key}).status_code == 429


def test_health_reports_the_safety_posture(client):
    d = client.get("/saas/health").json()
    assert d["status"] == "ok"
    assert "encryption" in d and "multi_tenant_mode" in d
    assert d["rate_limits_process_local"] is True, "an operator must know limits are per-worker"


# ------------------------------------------------------------- startup preflight

def test_a_hosted_app_refuses_to_start_without_encryption(monkeypatch):
    """
    The protection existed and nothing called it, which is the same as not having it.
    """
    from sovereign_os.saas.api import create_saas_app

    monkeypatch.delenv("SOVEREIGN_SAAS_SECRET", raising=False)
    with pytest.raises(SecretsNotConfigured):
        create_saas_app(TenantStore(tempfile.mkdtemp()), hosted=True)


def test_a_self_host_is_unaffected(monkeypatch):
    """Not hosting other people's keys means not being forced to configure for it."""
    from sovereign_os.saas.api import create_saas_app

    monkeypatch.delenv("SOVEREIGN_SAAS_SECRET", raising=False)
    assert create_saas_app(TenantStore(tempfile.mkdtemp())) is not None


def test_preflight_warns_when_byo_keys_could_silently_fall_back(monkeypatch):
    from sovereign_os.saas.api import hosted_preflight

    monkeypatch.setenv("SOVEREIGN_SAAS_SECRET", SECRET)
    monkeypatch.delenv("SOVEREIGN_MULTI_TENANT", raising=False)
    warnings = hosted_preflight(TenantStore(tempfile.mkdtemp(), secret=SECRET))
    assert any("SOVEREIGN_MULTI_TENANT" in w for w in warnings)


def test_preflight_is_quiet_when_configured_correctly(monkeypatch):
    from sovereign_os.saas.api import hosted_preflight

    monkeypatch.setenv("SOVEREIGN_SAAS_SECRET", SECRET)
    monkeypatch.setenv("SOVEREIGN_MULTI_TENANT", "1")
    assert hosted_preflight(TenantStore(tempfile.mkdtemp(), secret=SECRET)) == []


def test_preflight_flags_tenants_still_holding_plaintext(monkeypatch, tmp_path):
    import json

    from sovereign_os.saas.api import hosted_preflight

    monkeypatch.setenv("SOVEREIGN_SAAS_SECRET", SECRET)
    monkeypatch.setenv("SOVEREIGN_MULTI_TENANT", "1")
    root = tmp_path / "legacy"
    root.mkdir()
    (root / "tenants.json").write_text(json.dumps({"ten_1": {
        "id": "ten_1", "name": "Old", "api_key": "sk_ten_plain", "plan": "pro",
        "created_ts": 1.0,
        "config": {"anthropic_api_key": "sk-ant-PLAIN", "openai_api_key": "",
                   "stripe_api_key": "", "llm_provider": "", "x402_pay_to": "",
                   "charter_yaml": "", "earning_enabled": False},
    }}), encoding="utf-8")

    warnings = hosted_preflight(TenantStore(root, secret=SECRET))
    assert any("plaintext" in w for w in warnings)
