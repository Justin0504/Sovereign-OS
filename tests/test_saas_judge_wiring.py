"""
A hosted mission must be audited by the Judge, not by the stub.

Found by running a real mission through the hosted console: the deliverable was real
model output, and the audit reason read "Stub verification: output present". The tenant
engine was built before the tenant's keys were bound; ReviewEngine builds its Judge client
eagerly, that failed with no key in scope, and it fell back -- silently -- to StubAuditor,
which passes any non-empty output at a fixed 0.9. Workers build their clients lazily,
inside the key context, so nothing else looked wrong.

These drive the real entry point, run_tenant_mission, and record which auditor the
mission's own engine holds. No network: constructing the client does not call it, and the
mission body is replaced after construction.
"""

import asyncio
import tempfile

from sovereign_os.governance.engine import GovernanceEngine
from sovereign_os.saas.runtime import run_tenant_mission
from sovereign_os.saas.tenancy import TenantStore


def _run_and_capture_judge(monkeypatch, *, multi_tenant: bool) -> str:
    if multi_tenant:
        monkeypatch.setenv("SOVEREIGN_MULTI_TENANT", "1")
    else:
        monkeypatch.delenv("SOVEREIGN_MULTI_TENANT", raising=False)
    seen = {}

    async def capture(self, *a, **k):
        seen["judge"] = type(self._review_engine._judge).__name__
        return None, [], []

    monkeypatch.setattr(GovernanceEngine, "run_mission_with_audit", capture)
    store = TenantStore(tempfile.mkdtemp())
    t = store.create("Acme", plan="free")
    t.config.anthropic_api_key = "sk-ant-api03-" + "x" * 32
    store.update(t)
    asyncio.run(run_tenant_mission(t, store, "Summarize three agent frameworks."))
    return seen["judge"]


def test_hosted_mission_is_audited_by_the_judge(monkeypatch):
    assert _run_and_capture_judge(monkeypatch, multi_tenant=True) == "JudgeLLM"


def test_self_host_mission_with_a_tenant_key_is_audited_by_the_judge(monkeypatch):
    assert _run_and_capture_judge(monkeypatch, multi_tenant=False) == "JudgeLLM"
