"""
GovernanceEngine: The meta-orchestrator that runs missions.

Coordinates the CEO (Strategist) and CFO (Treasury): plan from goal,
then secure financial clearance for each task. dispatch(task_plan) runs
agents via WorkerRegistry and SovereignAuth, returning results for the Auditor.
"""

import logging
from typing import Any, Callable

from sovereign_os.agents.auth import Capability, PermissionDeniedError, SovereignAuth
from sovereign_os.agents.base import StubWorker, TaskInput, TaskResult
from sovereign_os.agents.registry import WorkerRegistry
from sovereign_os.auditor.base import AuditReport
from sovereign_os.auditor.review_engine import ReviewEngine
from sovereign_os.governance.exceptions import AuditFailureError, FiscalInsolvencyError
from sovereign_os.governance.strategist import PlannedTask, Strategist, StrategistLLMProtocol, TaskPlan
from sovereign_os.governance.treasury import Treasury
from sovereign_os.ledger.unified_ledger import UnifiedLedger
from sovereign_os.models.charter import Charter

logger = logging.getLogger(__name__)


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
        review_engine: ReviewEngine | None = None,
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

    async def dispatch(self, task_plan: TaskPlan) -> list[TaskResult]:
        """
        For each task in the plan: lookup Worker, check SovereignAuth,
        execute async, return list of TaskResult for the Auditor.
        """
        results: list[TaskResult] = []
        completed_ids: set[str] = set()

        for task in task_plan.tasks:
            for dep in task.dependencies:
                if dep not in completed_ids:
                    logger.warning("GOVERNANCE: Task %s depends on %s which is not completed; running anyway.", task.task_id, dep)
            agent_id = f"{task.required_skill}-{task.task_id}"
            capability = self._required_capability_for_skill(task.required_skill)
            if not self._auth.check_permission(agent_id, capability):
                raise PermissionDeniedError(
                    agent_id,
                    capability,
                    self._auth.get_trust_score(agent_id),
                    self._auth.get_threshold(capability),
                )
            worker = self._registry.get_worker(task.required_skill, agent_id)
            task_input = TaskInput(
                task_id=task.task_id,
                description=task.description,
                required_skill=task.required_skill,
                context={"goal_summary": task_plan.goal_summary},
            )
            if self._on_event:
                self._on_event("task_started", {"task_id": task.task_id, "agent_id": agent_id})
            result = await worker.execute(task_input)
            results.append(result)
            completed_ids.add(task.task_id)
            logger.info("GOVERNANCE: Task %s completed by [%s]; success=%s", task.task_id, agent_id, result.success)
            if self._on_event:
                self._on_event("task_finished", {"task_id": task.task_id, "agent_id": agent_id, "success": result.success})

        return results

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
