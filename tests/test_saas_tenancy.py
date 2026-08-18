"""Tests for the multi-tenant SaaS layer: plans, tenant store/isolation, BYO keys."""

import pytest

from sovereign_os.saas.plans import PLANS, get_plan
from sovereign_os.saas.runtime import build_tenant_engine, tenant_earning_active, tenant_llm_context
from sovereign_os.saas.tenancy import TenantConfig, TenantStore


# ------------------------------------------------------------------------ plans
def test_plans_features_and_limits():
    assert get_plan("free").allows("governance") and not get_plan("free").allows("earning")
    assert get_plan("team").allows("earning")
    assert get_plan("pro").max_missions_per_day > get_plan("free").max_missions_per_day
    assert get_plan("nonsense").name == "free"  # unknown -> default


# ----------------------------------------------------------------- tenant store
def test_create_get_and_api_key_auth(tmp_path):
    store = TenantStore(tmp_path)
    t = store.create("Acme", plan="pro", config=TenantConfig(anthropic_api_key="sk-ant-abc"))
    assert t.id.startswith("ten_") and t.api_key.startswith("sk_ten_") and t.plan == "pro"
    assert store.get(t.id) is t
    assert store.by_api_key(t.api_key).id == t.id
    assert store.by_api_key("wrong") is None and store.by_api_key("") is None
    assert [x.id for x in store.list()] == [t.id]


def test_public_never_leaks_secrets(tmp_path):
    store = TenantStore(tmp_path)
    t = store.create("Acme", config=TenantConfig(anthropic_api_key="sk-ant-supersecretkey12345",
                                                 stripe_api_key="sk_test_secret_stripe_key"))
    pub = t.public()
    assert "api_key" not in pub
    assert "supersecret" not in str(pub) and "secret_stripe" not in str(pub)
    assert pub["config"]["anthropic_api_key"].endswith("2345")  # masked prefix…suffix


def test_data_dir_isolation(tmp_path):
    store = TenantStore(tmp_path)
    a = store.create("A"); b = store.create("B")
    assert store.data_dir(a) != store.data_dir(b)
    assert store.data_dir(a).name == a.id


def test_persistence_reload(tmp_path):
    store = TenantStore(tmp_path)
    t = store.create("Acme", plan="team", config=TenantConfig(openai_api_key="sk-x"))
    store2 = TenantStore(tmp_path)
    got = store2.get(t.id)
    assert got is not None and got.name == "Acme" and got.plan == "team"
    assert got.config.openai_api_key == "sk-x"  # secrets persist to disk (returned only internally)


# ------------------------------------------------------------------- usage/limits
def test_can_run_requires_key(tmp_path):
    store = TenantStore(tmp_path)
    t = store.create("NoKey")
    ok, reason = store.can_run(t)
    assert ok is False and "key" in reason


def test_daily_spend_limit(tmp_path):
    store = TenantStore(tmp_path)
    t = store.create("Acme", plan="free", config=TenantConfig(openai_api_key="sk-x"))
    assert store.can_run(t)[0] is True
    store.record_mission(t.id, spend_cents=PLANS["free"].max_daily_spend_cents)
    ok, reason = store.can_run(t)
    assert ok is False and "spend" in reason


def test_daily_mission_limit(tmp_path):
    store = TenantStore(tmp_path)
    t = store.create("Acme", plan="free", config=TenantConfig(openai_api_key="sk-x"))
    for _ in range(PLANS["free"].max_missions_per_day):
        store.record_mission(t.id, spend_cents=0)
    ok, reason = store.can_run(t)
    assert ok is False and "mission" in reason
    assert store.usage(t.id).missions == PLANS["free"].max_missions_per_day


# ------------------------------------------------------------------- runtime
def test_engine_isolation(tmp_path):
    store = TenantStore(tmp_path)
    a = store.create("A", config=TenantConfig(anthropic_api_key="k"))
    b = store.create("B", config=TenantConfig(anthropic_api_key="k"))
    ea, la, _ = build_tenant_engine(a, store)
    eb, lb, _ = build_tenant_engine(b, store)
    assert la._path != lb._path
    assert str(la._path).endswith(a.id + "/ledger.jsonl")


def test_tenant_llm_context_binds_and_resets(tmp_path):
    from sovereign_os.llm.providers import _default_provider, _tenant_key_for

    store = TenantStore(tmp_path)
    t = store.create("Acme", config=TenantConfig(anthropic_api_key="sk-ant-xyz"))
    with tenant_llm_context(t):
        assert _tenant_key_for("anthropic") == "sk-ant-xyz"
        assert _default_provider() == "anthropic"
    assert _tenant_key_for("anthropic") is None  # reset after context


def test_earning_gated_by_plan_and_optin(tmp_path):
    store = TenantStore(tmp_path)
    free = store.create("F", config=TenantConfig(earning_enabled=True))          # plan lacks earning
    team = store.create("T", plan="team", config=TenantConfig(earning_enabled=True))
    team_off = store.create("T2", plan="team", config=TenantConfig(earning_enabled=False))
    assert tenant_earning_active(free) is False
    assert tenant_earning_active(team) is True
    assert tenant_earning_active(team_off) is False
