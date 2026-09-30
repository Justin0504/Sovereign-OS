"""
Tests for multi-tenant isolation of process-global state.

Both bugs pinned here were found by measurement, not review, and both were invisible
because everything still succeeded:

1. Cost calibration lived in a module-level singleton, so one tenant's settled tasks
   moved another tenant's budget ceiling — 3x on an identical task. The budget gate is
   the core safety mechanism, so having it set by someone else's workload is a
   governance failure, not a metrics smudge.

2. `contextvars` do not cross a `threading.Thread` boundary, so a mission dispatched to
   a worker thread lost the tenant's keys and fell back to the process env. In a hosted
   deployment that means the platform's key silently serves the tenant's work: the
   operator pays, and "your keys, never ours" quietly stops being true. It failed OPEN.
"""

import threading

import pytest

from sovereign_os.governance.cost_model import CostCalibrator
from sovereign_os.governance.strategist import PlannedTask
from sovereign_os.llm.providers import (
    TenantKeyMissing,
    _tenant_key_for,
    _tenant_keys,
    multi_tenant_mode,
    bind_tenant_context,
    set_tenant_keys,
)
from sovereign_os.saas.runtime import build_tenant_engine, tenant_llm_context
from sovereign_os.saas.tenancy import TenantConfig, TenantStore

SECRET = "test-deployment-secret"


@pytest.fixture
def store(tmp_path):
    return TenantStore(tmp_path / "tenants", secret=SECRET)


@pytest.fixture
def two_tenants(store):
    a = store.create("A", plan="pro", config=TenantConfig(anthropic_api_key="sk-ant-AAA"))
    b = store.create("B", plan="pro", config=TenantConfig(anthropic_api_key="sk-ant-BBB"))
    return store, a, b


def _task(skill="coding", budget=400_000):
    return PlannedTask(task_id="t", description="d", required_skill=skill,
                       estimated_token_budget=budget, priority="low")


def _teach(engine, n=40, estimate=100, actual=25, skill="coding"):
    """Settle `n` tasks that each cost a quarter of estimate."""
    for i in range(n):
        engine._task_estimate_cents[f"x{i}"] = estimate
        engine._task_raw_estimate_cents[f"x{i}"] = estimate
        engine._task_skill[f"x{i}"] = skill
        engine._reconcile_cost(f"x{i}", "w", actual)


# ------------------------------------------------- calibration state per tenant

def test_one_tenants_history_does_not_move_anothers_budget(two_tenants):
    store, a, b = two_tenants
    eng_a, _, _ = build_tenant_engine(a, store)
    eng_b, _, _ = build_tenant_engine(b, store)

    before = eng_b._default_cost_converter(_task())
    _teach(eng_a)
    after = eng_b._default_cost_converter(_task())

    assert after == before, "tenant B's budget ceiling must not move with A's workload"
    assert eng_a._calibrator.factor("coding") < 0.5     # A did learn
    assert eng_b._calibrator.factor("coding") == 1.0    # B learned nothing


def test_each_tenant_gets_a_distinct_calibrator(two_tenants):
    store, a, b = two_tenants
    eng_a, _, _ = build_tenant_engine(a, store)
    eng_b, _, _ = build_tenant_engine(b, store)
    assert eng_a._calibrator is not eng_b._calibrator
    assert eng_a._calibrator is not None


def test_a_tenants_history_does_not_leak_into_the_process_global(two_tenants):
    """The singleton must stay untouched, or a self-host in the same process inherits it."""
    from sovereign_os.governance.cost_model import cost_stats, reset_cost

    reset_cost()
    store, a, _ = two_tenants
    eng_a, _, _ = build_tenant_engine(a, store)
    _teach(eng_a)
    assert cost_stats("coding").n == 0
    reset_cost()


def test_calibration_survives_a_restart(two_tenants, tmp_path):
    """In-memory-only learning means every deploy reverts to the cold heuristic."""
    store, a, _ = two_tenants
    eng_a, _, _ = build_tenant_engine(a, store)
    _teach(eng_a)
    eng_a._calibrator.save(store.data_dir(a) / "calibration.json")

    reopened, _, _ = build_tenant_engine(a, store)
    assert reopened._calibrator.factor("coding") == pytest.approx(
        eng_a._calibrator.factor("coding"), rel=1e-6)


def test_saved_history_is_written_atomically(tmp_path):
    """A crash mid-write must not truncate the learned history."""
    cal = CostCalibrator()
    for _ in range(10):
        cal.record("coding", 100, 50)
    path = tmp_path / "nested" / "calibration.json"
    cal.save(path)
    assert path.exists()
    assert not list(path.parent.glob("*.tmp"))
    assert CostCalibrator.load(path).factor("coding") == cal.factor("coding")


def test_corrupt_history_does_not_crash_startup(tmp_path):
    path = tmp_path / "calibration.json"
    path.write_text("{not json", encoding="utf-8")
    assert CostCalibrator.load(path).factor("coding") == 1.0


def test_single_tenant_still_uses_the_process_global(tmp_path):
    """A self-host must keep its existing behaviour — no calibrator injected."""
    from sovereign_os.governance.cost_model import cost_stats, reset_cost
    from sovereign_os.governance.engine import GovernanceEngine
    from sovereign_os.ledger.unified_ledger import UnifiedLedger
    from sovereign_os.models.charter import load_charter

    reset_cost()
    engine = GovernanceEngine(load_charter("charter.default.yaml"), UnifiedLedger())
    assert engine._calibrator is None
    _teach(engine, n=5)
    assert cost_stats("coding").n == 5
    reset_cost()


# ------------------------------------------------- key context across threads

def test_contextvars_do_not_cross_a_thread_by_default(two_tenants):
    """The underlying behaviour the fail-closed guard exists to cover."""
    store, a, _ = two_tenants
    seen = {}
    with tenant_llm_context(a):
        seen["request"] = (_tenant_keys.get() or {}).get("anthropic")
        t = threading.Thread(
            target=lambda: seen.__setitem__("thread", (_tenant_keys.get() or {}).get("anthropic")))
        t.start()
        t.join()
    assert seen["request"] == "sk-ant-AAA"
    assert seen["thread"] is None


def test_capturing_inside_the_thread_does_not_work(two_tenants):
    """
    Pins the trap: copy_context() called from inside the worker copies the worker's own
    empty context. The helper has to capture in the originating context instead — this
    failure is the same shape as the bug the helper exists to fix.
    """
    import contextvars

    store, a, _ = two_tenants
    seen = {}

    def work():
        seen["thread"] = (_tenant_keys.get() or {}).get("anthropic")

    with tenant_llm_context(a):
        t = threading.Thread(target=lambda: contextvars.copy_context().run(work))
        t.start()
        t.join()
    assert seen["thread"] is None


def test_context_can_be_carried_across_a_thread(two_tenants):
    store, a, _ = two_tenants
    seen = {}

    def work():
        seen["thread"] = (_tenant_keys.get() or {}).get("anthropic")

    with tenant_llm_context(a):
        t = threading.Thread(target=bind_tenant_context(work))
        t.start()
        t.join()
    assert seen["thread"] == "sk-ant-AAA"


# ------------------------------------------------- fail closed, not open

def test_multi_tenant_mode_is_off_unless_asked(monkeypatch):
    monkeypatch.delenv("SOVEREIGN_MULTI_TENANT", raising=False)
    assert multi_tenant_mode() is False
    assert _tenant_key_for("anthropic") is None          # self-host: env fallback is fine


def test_a_lost_context_raises_instead_of_using_the_platform_key(monkeypatch):
    """
    The important one. Without this the mission succeeds on the operator's key and
    nothing anywhere reports a problem.
    """
    monkeypatch.setenv("SOVEREIGN_MULTI_TENANT", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-PLATFORM")
    with pytest.raises(TenantKeyMissing):
        _tenant_key_for("anthropic")


def test_a_context_missing_the_needed_provider_also_raises(monkeypatch):
    monkeypatch.setenv("SOVEREIGN_MULTI_TENANT", "1")
    token = set_tenant_keys({"anthropic": "sk-ant-AAA"})
    try:
        assert _tenant_key_for("anthropic") == "sk-ant-AAA"
        with pytest.raises(TenantKeyMissing):
            _tenant_key_for("openai")        # tenant has no OpenAI key; must not fall back
    finally:
        _tenant_keys.reset(token)


def test_a_bound_context_resolves_normally_in_multi_tenant_mode(monkeypatch, two_tenants):
    monkeypatch.setenv("SOVEREIGN_MULTI_TENANT", "1")
    store, a, b = two_tenants
    with tenant_llm_context(a):
        assert _tenant_key_for("anthropic") == "sk-ant-AAA"
    with tenant_llm_context(b):
        assert _tenant_key_for("anthropic") == "sk-ant-BBB"


def test_threaded_dispatch_fails_closed_in_multi_tenant_mode(monkeypatch, two_tenants):
    """End to end: the exact production shape — a mission handed to a worker thread."""
    monkeypatch.setenv("SOVEREIGN_MULTI_TENANT", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-PLATFORM")
    store, a, _ = two_tenants
    result = {}

    def work():
        try:
            result["key"] = _tenant_key_for("anthropic")
        except TenantKeyMissing as e:
            result["raised"] = str(e)

    with tenant_llm_context(a):
        t = threading.Thread(target=work)      # context deliberately NOT carried
        t.start()
        t.join()

    assert "raised" in result, "must refuse rather than serve the tenant on the platform key"
    assert result.get("key") != "sk-ant-PLATFORM"
