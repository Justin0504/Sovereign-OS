"""
A mission must not leak authority or free compute when it fails, or when it runs
alongside another tenant's.

Two defects, both found by driving the path rather than reading it.

**The broker was a module global.** `_active_grant` in delegation_gate was correctly a
ContextVar; `_BROKER`, the thing that interprets a grant, was not. Missions are async and
the hosted control plane runs them concurrently in one process, so whoever called
set_broker last won. Measured: two concurrent missions, and the longer one saw None after
its await because the shorter one had finished and reset the global. In permissive mode
that silently means ungoverned; under SOVEREIGN_STRICT_DELEGATION=1, which the hosted
deployment sets, it means the first tenant's legitimate handoffs are refused.

**The teardown was on the success path.** set_broker(None) and record_mission sat after
the await, so a mission that raised -- a provider 503, a timeout -- left the broker
installed for whatever request came next and recorded no usage at all. Unmetered failures
mean a tenant can retry without limit, burning real tokens against a quota that never
moves.
"""

import asyncio
import tempfile

import pytest

from sovereign_os.agents.delegation_gate import get_broker, set_broker
from sovereign_os.ledger.unified_ledger import UnifiedLedger
from sovereign_os.saas.tenancy import TenantStore


@pytest.fixture(autouse=True)
def clean_broker():
    set_broker(None)
    yield
    set_broker(None)


def _tenant(store, name="Acme"):
    t = store.create(name, plan="free")
    t.config.anthropic_api_key = "sk-ant-api03-" + "x" * 32
    store.update(t)
    return t


class _Exploding:
    """An engine that fails the way a provider outage does: after setup, during the run."""

    _delegation = "tenant-tree"
    _calibrator = None

    async def run_mission_with_audit(self, *a, **k):
        raise RuntimeError("provider 503")


# ------------------------------------------------------- the broker is context-scoped

def test_concurrent_missions_do_not_see_each_others_broker():
    """
    The probe that found it. Without a ContextVar the longer mission sees whatever the
    shorter one left behind -- None here, another tenant's tree in production.
    """
    seen = {}

    async def mission(name, hold):
        set_broker(f"{name}-tree")
        await asyncio.sleep(hold)
        seen[name] = get_broker()
        set_broker(None)

    async def both():
        await asyncio.gather(mission("A", 0.05), mission("B", 0.01))

    asyncio.run(both())
    assert seen["A"] == "A-tree", "tenant A lost its broker to tenant B's teardown"
    assert seen["B"] == "B-tree"


def test_a_broker_set_outside_a_task_is_still_visible_inside_one():
    """
    The single-tenant self-host sets the broker once at startup and never again. A
    ContextVar must not break that: a value set in the root context is inherited.
    """
    set_broker("startup-tree")

    async def inner():
        return get_broker()

    assert asyncio.run(inner()) == "startup-tree"


# ------------------------------------------------- failure leaves nothing behind

def test_a_failed_mission_releases_the_broker():
    import sovereign_os.saas.runtime as rt

    store = TenantStore(tempfile.mkdtemp())
    t = _tenant(store)
    real = rt.build_tenant_engine
    rt.build_tenant_engine = lambda _t, _s: (_Exploding(), UnifiedLedger(), None)
    try:
        with pytest.raises(RuntimeError):
            asyncio.run(rt.run_tenant_mission(t, store, "do a thing"))
        assert get_broker() is None, \
            "a failed mission left its delegation tree installed for the next request"
    finally:
        rt.build_tenant_engine = real


def test_a_failed_mission_still_counts_against_the_quota():
    """
    Otherwise a tenant whose missions fail retries forever: each attempt burns real
    tokens before it fails, and the quota never moves.
    """
    import sovereign_os.saas.runtime as rt

    store = TenantStore(tempfile.mkdtemp())
    t = _tenant(store)
    real = rt.build_tenant_engine
    rt.build_tenant_engine = lambda _t, _s: (_Exploding(), UnifiedLedger(), None)
    try:
        for _ in range(3):
            with pytest.raises(RuntimeError):
                asyncio.run(rt.run_tenant_mission(t, store, "do a thing"))
        assert store.usage(t.id).missions == 3
    finally:
        rt.build_tenant_engine = real


def test_spend_before_a_failure_is_metered():
    """Tokens burned before the exception were still bought."""
    import sovereign_os.saas.runtime as rt

    store = TenantStore(tempfile.mkdtemp())
    t = _tenant(store)

    class _SpendsThenFails(_Exploding):
        def __init__(self, ledger):
            self._ledger = ledger

        async def run_mission_with_audit(self, *a, **k):
            # Metering reads token spend, not USD movements, so this has to be the
            # dimension the quota is actually denominated in.
            self._ledger.record_token("claude-haiku-4-5", 1200, 300,
                                      estimated_usd_cents=42, task_id="t1")
            raise RuntimeError("provider 503")

    real = rt.build_tenant_engine

    def fake(_t, _s):
        led = UnifiedLedger()
        led.record_usd(1000, purpose="seed")
        return _SpendsThenFails(led), led, None

    rt.build_tenant_engine = fake
    try:
        with pytest.raises(RuntimeError):
            asyncio.run(rt.run_tenant_mission(t, store, "do a thing"))
        assert store.usage(t.id).spend_cents > 0, "spend before the failure went unbilled"
    finally:
        rt.build_tenant_engine = real
