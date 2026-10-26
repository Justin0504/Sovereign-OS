"""
SE-02, revocation survival: state that keeps acting after the lease is revoked and the
task has ended.

The threat model's examples are a background process, a scheduled job, or a cached
credential. This system has no background-process or scheduled-job surface -- workers are
awaited to completion and nothing is spawned to outlive a task (no subprocess, no detached
thread, no scheduler) -- so for this system the attack reduces to the cached-credential
case: an authorization that was issued for a task and still works after it.

There are two authority stores, and the defense has to cover both:

  * Delegation grants. authorize() re-checks liveness on every call, and the engine
    revokes a task's grants in its finally (release_task). A held grant_id cannot act
    after revocation. This was already wired and tested.

  * JIT capability leases on SovereignAuth. revoke_task_leases -- "privileges drop to
    baseline when not actively engaged" -- was defined and tested but NOT called when a
    task ended. A lease outlived its task. This is the "wire the defense" failure: a
    teardown that exists, passes its unit test, and protects nothing because production
    never reaches it. The engine now calls it in the same finally as release_task; the
    load-bearing test below drives the real task lifecycle and asserts the lease is dead
    afterward, not that the function works in isolation.
"""

import asyncio

import pytest

from sovereign_os.agents.auth import Capability, SovereignAuth
from sovereign_os.agents.base import BaseWorker, TaskResult
from sovereign_os.agents.registry import WorkerRegistry
from sovereign_os.governance.engine import GovernanceEngine
from sovereign_os.governance.strategist import PlannedTask, TaskPlan
from sovereign_os.ledger.unified_ledger import UnifiedLedger
from sovereign_os.models.charter import Charter, FiscalBoundaries

WRITE = Capability.WRITE_FILES


# ------------------------------------------- the lease store, in isolation (regression)

def test_a_revoked_lease_cannot_be_used():
    auth = SovereignAuth()
    auth._set_score("w", 100)
    auth.grant_lease("w", WRITE, task_id="t1", ttl_seconds=3600, max_uses=10)
    assert auth.use_lease("w", WRITE, "t1") is True
    auth.revoke_task_leases("t1")
    assert auth.use_lease("w", WRITE, "t1") is False


# -------------------------------------- the wiring: a real task tears its leases down

class _LeasingWorker(BaseWorker):
    """Acquires a JIT lease during the task, the way a real worker would before a
    privileged action."""

    skill = "research"

    def __init__(self, *a, auth=None, **k):
        super().__init__(*a, **k)
        self._auth = auth

    async def execute(self, task):
        self._auth._set_score(self.agent_id, 100)
        self._auth.grant_lease(self.agent_id, WRITE, task_id=task.task_id,
                               ttl_seconds=3600, max_uses=100)
        # It can write during the task.
        assert self._auth.use_lease(self.agent_id, WRITE, task.task_id) is True
        return TaskResult(task_id=task.task_id, success=True, output="done")


def test_a_lease_does_not_survive_the_task_that_issued_it():
    """
    The load-bearing one, driven through the engine rather than calling the teardown
    directly. Before the fix, the lease was still usable here because nothing revoked it
    when the task ended.
    """
    auth = SovereignAuth()
    charter = Charter(mission="m", fiscal_boundaries=FiscalBoundaries(max_budget_usd=100.0))
    ledger = UnifiedLedger()
    ledger.record_usd(100_000, purpose="seed")

    worker = None

    class _Factory(_LeasingWorker):
        def __init__(self, *a, **k):
            super().__init__(*a, auth=auth, **k)
            nonlocal worker
            worker = self

    reg = WorkerRegistry(charter)
    reg.register("research", _Factory)
    engine = GovernanceEngine(charter, ledger, auth=auth, registry=reg)

    plan = TaskPlan(goal_summary="g", tasks=[
        PlannedTask(task_id="t1", description="do", dependencies=[],
                    required_skill="research", estimated_token_budget=2000, priority="high")])
    asyncio.run(engine.dispatch(plan))

    assert worker is not None, "the leasing worker never ran"
    agent_id = worker.agent_id
    # The task is over. The lease it acquired must no longer authorize anything.
    assert auth.use_lease(agent_id, WRITE, "t1") is False, \
        "a JIT lease outlived the task that issued it"
    assert auth.has_active_lease(agent_id, WRITE, "t1") is False


def test_teardown_runs_even_when_the_task_raises():
    """A task that throws must not leave its lease live -- the finally has to cover it."""
    auth = SovereignAuth()
    charter = Charter(mission="m", fiscal_boundaries=FiscalBoundaries(max_budget_usd=100.0))
    ledger = UnifiedLedger()
    ledger.record_usd(100_000, purpose="seed")

    class _ThrowingLeaser(BaseWorker):
        skill = "research"

        async def execute(self, task):
            auth._set_score(self.agent_id, 100)
            auth.grant_lease(self.agent_id, WRITE, task_id=task.task_id,
                             ttl_seconds=3600, max_uses=100)
            raise RuntimeError("boom after acquiring the lease")

    reg = WorkerRegistry(charter)
    reg.register("research", _ThrowingLeaser)
    engine = GovernanceEngine(charter, ledger, auth=auth, registry=reg)
    plan = TaskPlan(goal_summary="g", tasks=[
        PlannedTask(task_id="t1", description="do", dependencies=[],
                    required_skill="research", estimated_token_budget=2000, priority="high")])
    # dispatch swallows per-task errors into a failed TaskResult; the lease teardown
    # still has to have run.
    asyncio.run(engine.dispatch(plan))
    # No active lease for any agent on this task.
    assert not any(
        auth.has_active_lease(aid, WRITE, "t1")
        for aid in {"research-t1", "research-task-1", "research"}), \
        "a throwing task left its lease live"
