"""
GovernanceEngine: The meta-orchestrator that runs missions.

Coordinates the CEO (Strategist) and CFO (Treasury): plan from goal,
then secure financial clearance for each task. dispatch(task_plan) runs
agents via WorkerRegistry and SovereignAuth with async DAG execution;
TaskLifecycleManager tracks PENDING/RUNNING/COMPLETED/FAILED; structured JSON logging.
"""

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any, Callable

from sovereign_os.agents.auth import Capability, PermissionDeniedError, SovereignAuth
from sovereign_os.agents.base import StubWorker, TaskInput, TaskResult
from sovereign_os.agents.registry import WorkerRegistry
from sovereign_os.auditor.base import AuditReport
from sovereign_os.governance.exceptions import AuditFailureError, FiscalInsolvencyError

if TYPE_CHECKING:
    from sovereign_os.auditor.review_engine import ReviewEngine
from sovereign_os.governance.lifecycle import TaskLifecycleManager, TaskState
from sovereign_os.governance.rate_limit import get_global_rate_limiter
from sovereign_os.governance.strategist import PlannedTask, Strategist, StrategistLLMProtocol, TaskPlan
from sovereign_os.mcp.tool_mapping import get_tools_for_skill
from sovereign_os.governance.treasury import Treasury
from sovereign_os.ledger.unified_ledger import UnifiedLedger
from sovereign_os.models.charter import Charter

logger = logging.getLogger(__name__)


def _log_task_transition(task_id: str, state: str, **extra: Any) -> None:
    """Structured JSON log for task transitions (parallel flow debugging)."""
    payload = {"event": "task_transition", "task_id": task_id, "state": state, **extra}
    logger.info("%s", json.dumps(payload, default=str))


# Heuristic: convert estimated token budget to approximate USD cents for CFO check.
# (e.g. ~$0.01 per 1k tokens for cheap model; adjust per deployment.)
DEFAULT_CENTS_PER_THOUSAND_TOKENS = 10


def _task_estimated_cost_cents(
    task: PlannedTask,
    cents_per_thousand_tokens: int = DEFAULT_CENTS_PER_THOUSAND_TOKENS,
) -> int:
    """Derive estimated cost in cents from task's token budget."""
    return max(1, (task.estimated_token_budget * cents_per_thousand_tokens) // 1000)


class GovernanceEngine:
    """
    Main orchestrator: Charter + Ledger, runs missions via Strategist and Treasury.

    run_mission(goal_text): get plan -> CFO approval per task -> on success,
    log Strategic Intent and return approved plan for Agent Dispatch.
    """

    def __init__(
        self,
        charter: Charter,
        ledger: UnifiedLedger,
        *,
        strategist_llm: StrategistLLMProtocol | None = None,
        cost_converter: Callable[[PlannedTask], int] | None = None,
        auth: SovereignAuth | None = None,
        registry: WorkerRegistry | None = None,
        review_engine: "ReviewEngine | None" = None,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self._charter = charter
        self._ledger = ledger
        self._on_event = on_event
        self._strategist = Strategist(charter, llm_client=strategist_llm)
        self._treasury = Treasury(charter, ledger)
        self._cost_converter = cost_converter or (
            lambda t: _task_estimated_cost_cents(t, DEFAULT_CENTS_PER_THOUSAND_TOKENS)
        )
        self._auth = auth or SovereignAuth()
        self._registry = registry or self._default_registry()
        self._review_engine = review_engine

    def _default_registry(self) -> WorkerRegistry:
        r = WorkerRegistry(self._charter)
        r.set_default(StubWorker)
        return r

    async def run_mission(self, goal_text: str) -> TaskPlan:
        """
        Execute the mission pipeline: plan -> fiscal clearance -> Strategic Intent.

        1. CEO (Strategist) produces a TaskPlan.
        2. For each task, CFO (Treasury) approves budget; on first denial, aborts with FiscalInsolvencyError.
        3. If all cleared, log Strategic Intent and return the plan for Agent Dispatch (Phase 3).
        """
        logger.info("GOVERNANCE: Mission started. Goal: %s", (goal_text[:200] + "..." if len(goal_text) > 200 else goal_text))
        plan = await self._strategist.create_plan(goal_text)

        for task in plan.tasks:
            estimated_cents = self._cost_converter(task)
            try:
                self._treasury.approve_task(
                    estimated_cents,
                    task_id=task.task_id,
                    purpose=task.description[:100] or "task",
                )
            except FiscalInsolvencyError as e:
                logger.error(
                    "GOVERNANCE: Mission aborted — CFO denied budget for task %s. %s",
                    task.task_id,
                    str(e),
                )
                raise

        logger.info(
            "GOVERNANCE: Strategic Intent logged. %d tasks approved; ready for Agent Dispatch (Phase 3).",
            len(plan.tasks),
        )
        if self._on_event:
            self._on_event("plan_created", {"goal": goal_text, "tasks": [{"task_id": t.task_id, "required_skill": t.required_skill} for t in plan.tasks]})
        return plan

    def _required_capability_for_skill(self, required_skill: str) -> Capability:
        """Map task skill to the capability checked before execution."""
        s = required_skill.strip().lower()
        if s in ("code", "write"):
            return Capability.WRITE_FILES
        if s in ("execute", "shell"):
            return Capability.EXECUTE_SHELL
        if s in ("spend", "pay"):
            return Capability.SPEND_USD
        return Capability.READ_FILES

    def _ready_task_ids(self, task_plan: TaskPlan, completed_ids: set[str]) -> list[str]:
        """Task IDs that have all dependencies satisfied and are not yet completed."""
        task_by_id = {t.task_id: t for t in task_plan.tasks}
        ready: list[str] = []
        for task in task_plan.tasks:
            if task.task_id in completed_ids:
                continue
            if all(dep in completed_ids for dep in task.dependencies):
                ready.append(task.task_id)
        return ready

    async def _run_one_task(
        self,
        task: PlannedTask,
        task_plan: TaskPlan,
        lifecycle: TaskLifecycleManager,
        result_by_id: dict[str, TaskResult],
    ) -> None:
        """Execute a single task: auth check, rate limit, worker.execute, then record result and lifecycle."""
        agent_id = f"{task.required_skill}-{task.task_id}"
        capability = self._required_capability_for_skill(task.required_skill)
        if not self._auth.check_permission(agent_id, capability):
            lifecycle.set_failed(task.task_id, agent_id=agent_id, error="permission_denied")
            raise PermissionDeniedError(
                agent_id,
                capability,
                self._auth.get_trust_score(agent_id),
                self._auth.get_threshold(capability),
            )
        limiter = get_global_rate_limiter()
        if limiter is not None:
            await limiter.acquire()
        lifecycle.set_running(task.task_id, agent_id=agent_id)
        if self._on_event:
            self._on_event("task_started", {"task_id": task.task_id, "agent_id": agent_id})
        worker = self._registry.get_worker(task.required_skill, agent_id)
        task_input = TaskInput(
            task_id=task.task_id,
            description=task.description,
            required_skill=task.required_skill,
            context={
                "goal_summary": task_plan.goal_summary,
                "mcp_tool_names": get_tools_for_skill(task.required_skill),
            },
        )
        try:
            result = await worker.execute(task_input)
            result_by_id[task.task_id] = result
            lifecycle.set_completed(task.task_id, agent_id=agent_id, success=result.success)
            _log_task_transition(task.task_id, TaskState.COMPLETED.value, agent_id=agent_id, success=result.success)
            if self._on_event:
                self._on_event("task_finished", {"task_id": task.task_id, "agent_id": agent_id, "success": result.success})
        except PermissionDeniedError:
            lifecycle.set_failed(task.task_id, agent_id=agent_id, error="permission_denied")
            result_by_id[task.task_id] = TaskResult(task_id=task.task_id, success=False, output="", metadata={"error": "permission_denied"})
            raise
        except Exception as e:
            lifecycle.set_failed(task.task_id, agent_id=agent_id, error=str(e))
            _log_task_transition(task.task_id, TaskState.FAILED.value, agent_id=agent_id, error=str(e))
            result_by_id[task.task_id] = TaskResult(task_id=task.task_id, success=False, output="", metadata={"error": str(e)})
            if self._on_event:
                self._on_event("task_finished", {"task_id": task.task_id, "agent_id": agent_id, "success": False})

    async def dispatch(self, task_plan: TaskPlan) -> list[TaskResult]:
        """
        DAG-aware dispatch: tasks with no remaining dependencies run concurrently via asyncio.gather.
        TaskLifecycleManager tracks PENDING -> RUNNING -> COMPLETED | FAILED; structured JSON logging.
        """
        task_by_id = {t.task_id: t for t in task_plan.tasks}
        lifecycle = TaskLifecycleManager([t.task_id for t in task_plan.tasks])
        result_by_id: dict[str, TaskResult] = {}

        while not lifecycle.all_done():
            completed = lifecycle.completed_ids()
            ready_ids = self._ready_task_ids(task_plan, completed)
            if not ready_ids:
                if not lifecycle.all_done():
                    _log_task_transition("_dag", "stall", completed=list(completed), snapshot=lifecycle.snapshot())
                break
            tasks_to_run = [task_by_id[tid] for tid in ready_ids]
            coros = [
                self._run_one_task(task, task_plan, lifecycle, result_by_id)
                for task in tasks_to_run
            ]
            await asyncio.gather(*coros, return_exceptions=False)

        # Return results in plan order for Auditor (include failed/never-run placeholders)
        ordered: list[TaskResult] = []
        for t in task_plan.tasks:
            if t.task_id in result_by_id:
                ordered.append(result_by_id[t.task_id])
            else:
                ordered.append(
                    TaskResult(
                        task_id=t.task_id,
                        success=False,
                        output="",
                        metadata={"error": "not_run", "state": lifecycle.get_state(t.task_id).value},
                    )
                )
        return ordered

    async def run_mission_with_audit(
        self,
        goal_text: str,
        *,
        abort_on_audit_failure: bool = True,
    ) -> tuple[TaskPlan, list[TaskResult], list[AuditReport]]:
        """
        Full pipeline: run_mission -> dispatch -> audit each result.
        On audit pass: SovereignAuth.record_audit_success(agent_id).
        On audit fail: SovereignAuth.record_audit_failure(agent_id); optionally abort (raise AuditFailureError).
        """
        plan = await self.run_mission(goal_text)
        results = await self.dispatch(plan)
        reports: list[AuditReport] = []

        if self._review_engine is None:
            logger.warning("GOVERNANCE: No ReviewEngine configured; skipping audit.")
            return plan, results, reports

        for task, result in zip(plan.tasks, results, strict=True):
            report = await self._review_engine.audit_task(task, result)
            reports.append(report)
            agent_id = f"{task.required_skill}-{task.task_id}"

            if report.passed:
                self._auth.record_audit_success(agent_id)
                logger.info(
                    "AUDIT: Task [%s] verified against KPI [%s]. Quality Score: %.2f.",
                    task.task_id,
                    report.kpi_name,
                    report.score,
                )
            else:
                self._auth.record_audit_failure(agent_id)
                logger.critical(
                    "AUDIT CRITICAL: Task [%s] failed verification. Reason: %s",
                    task.task_id,
                    report.reason,
                )
            if self._on_event:
                self._on_event("task_audited", {"task_id": task.task_id, "agent_id": agent_id, "passed": report.passed, "score": report.score, "reason": report.reason})
            if not report.passed and abort_on_audit_failure:
                raise AuditFailureError(task.task_id, report.reason, report=report)

        return plan, results, reports
