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
from sovereign_os.agents.content_workers import (
    ArticleWriterWorker,
    AssistantChatWorker,
    EmailWriterWorker,
    HelpContentWorker,
    MeetingMinutesWorker,
    ProblemSolverWorker,
    RewritePolishWorker,
    SocialPostWorker,
    TranslateWorker,
)
from sovereign_os.agents.code_workers import CodeAssistantWorker, CodeReviewWorker
from sovereign_os.agents.specialist_workers import DataAnalysisWorker, DesignBriefWorker
from sovereign_os.agents.ops_workers import (
    ExtractStructuredWorker,
    InfoCollectorWorker,
    SpecWriterWorker,
)
from sovereign_os.agents.reply_worker import ReplyWorker
from sovereign_os.agents.research_worker import ResearchWorker
from sovereign_os.agents.summarizer_worker import SummarizerWorker
from sovereign_os.auditor.base import AuditReport
from sovereign_os.governance.exceptions import AuditFailureError, FiscalInsolvencyError, UnprofitableJobError

if TYPE_CHECKING:
    from sovereign_os.auditor.review_engine import ReviewEngine
    from sovereign_os.compliance.hooks import ComplianceHook
    from sovereign_os.governance.auction import BiddingEngine
    from sovereign_os.memory.manager import MemoryManager
    from sovereign_os.mcp.tool_graph import MCPToolGraph
from sovereign_os.governance.lifecycle import TaskLifecycleManager, TaskState
from sovereign_os.governance.auction import RequestForProposal
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
        memory_manager: "MemoryManager | None" = None,
        bidding_engine: "BiddingEngine | None" = None,
        mcp_tool_graph: "MCPToolGraph | None" = None,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
        compliance_hook: "ComplianceHook | None" = None,
        spend_threshold_cents: int = 0,
        compliance_auto_proceed: bool = False,
        budget_policy: "Any | None" = None,
    ) -> None:
        self._charter = charter
        self._ledger = ledger
        self._memory_manager = memory_manager
        self._mcp_tool_graph = mcp_tool_graph
        self._on_event = on_event
        self._strategist = Strategist(charter, llm_client=strategist_llm)
        self._treasury = Treasury(
            charter,
            ledger,
            compliance_hook=compliance_hook,
            spend_threshold_cents=spend_threshold_cents,
            compliance_auto_proceed=compliance_auto_proceed,
            budget_policy=budget_policy,
        )
        self._cost_converter = cost_converter or self._default_cost_converter
        self._auth = auth or SovereignAuth()
        self._registry = registry or self._default_registry()
        self._review_engine = review_engine
        self._bidding_engine = bidding_engine
        # Per-task CFO pre-estimate (cents), used to reconcile actual vs budgeted spend.
        self._task_estimate_cents: dict[str, int] = {}
        # Cumulative actual spend within the current dispatch run (cents), for budget halt.
        self._mission_spent_cents: int = 0

    def _default_registry(self) -> WorkerRegistry:
        r = WorkerRegistry(self._charter, mcp_tool_graph=self._mcp_tool_graph)
        r.register("summarize", SummarizerWorker)
        r.register("research", ResearchWorker)
        r.register("reply", ReplyWorker)
        # Content / common freelance jobs
        r.register("write_article", ArticleWriterWorker)
        r.register("solve_problem", ProblemSolverWorker)
        r.register("write_email", EmailWriterWorker)
        r.register("write_post", SocialPostWorker)
        r.register("help_docs", HelpContentWorker)
        r.register("meeting_minutes", MeetingMinutesWorker)
        r.register("translate", TranslateWorker)
        r.register("rewrite_polish", RewritePolishWorker)
        # Ops / structured deliverables
        r.register("collect_info", InfoCollectorWorker)
        r.register("extract_structured", ExtractStructuredWorker)
        r.register("spec_writer", SpecWriterWorker)
        r.register("assistant_chat", AssistantChatWorker)
        r.register("code_assistant", CodeAssistantWorker)
        r.register("code_review", CodeReviewWorker)
        # Top-tier specialists for high-frequency platform categories
        r.register("design_brief", DesignBriefWorker)
        r.register("data_analysis", DataAnalysisWorker)
        try:
            from sovereign_os.agents.user_workers import get_user_workers
            for skill_name, worker_cls in get_user_workers():
                r.register(skill_name, worker_cls)
        except Exception as e:
            logger.warning("Could not load user workers: %s", e)
        r.set_default(StubWorker)
        return r

    # Actual spend may exceed the CFO estimate by this fraction before it counts
    # as an overrun (estimates are approximate; small drift is expected).
    BUDGET_OVERRUN_TOLERANCE = 0.25

    def _mission_cost_cap_cents(self) -> int:
        """Per-mission cumulative spend ceiling in cents (0 = disabled)."""
        return int(getattr(self._charter.fiscal_boundaries, "max_mission_cost_usd", 0.0) * 100)

    def _halt_remaining_for_budget(
        self,
        task_plan: TaskPlan,
        lifecycle: TaskLifecycleManager,
        result_by_id: dict[str, TaskResult],
        cap_cents: int,
    ) -> None:
        """Stop the mission: mark every not-yet-completed task as halted and emit an event."""
        completed = lifecycle.completed_ids()
        halted: list[str] = []
        for t in task_plan.tasks:
            if t.task_id in completed or t.task_id in result_by_id:
                continue
            result_by_id[t.task_id] = TaskResult(
                task_id=t.task_id, success=False, output="",
                metadata={"error": "budget_halt"},
            )
            lifecycle.set_failed(t.task_id, error="budget_halt")
            halted.append(t.task_id)
        logger.warning(
            "GOVERNANCE CFO: Mission budget exhausted — spent %d cents >= cap %d cents. Halted %d task(s): %s",
            self._mission_spent_cents, cap_cents, len(halted), ", ".join(halted) or "(none)",
        )
        if self._on_event:
            self._on_event(
                "mission_budget_exhausted",
                {"spent_cents": self._mission_spent_cents, "cap_cents": cap_cents, "halted_task_ids": halted},
            )

    def _reconcile_cost(self, task_id: str, agent_id: str, actual_cents: int) -> None:
        """
        Compare actual token cost against the CFO's pre-approved estimate.

        When actual materially exceeds the estimate, dock the agent's TrustScore
        (record_budget_overrun) and emit a `budget_overrun` event. This closes the
        estimate→actual loop so chronic over-spenders lose autonomy over time.
        """
        estimate = self._task_estimate_cents.get(task_id)
        if not estimate or estimate <= 0:
            return
        threshold = estimate * (1.0 + self.BUDGET_OVERRUN_TOLERANCE)
        if actual_cents > threshold:
            logger.warning(
                "GOVERNANCE CFO: Task [%s] overran budget — actual %d cents vs estimate %d cents (+%.0f%% tolerance).",
                task_id, actual_cents, estimate, self.BUDGET_OVERRUN_TOLERANCE * 100,
            )
            if self._auth is not None and agent_id:
                self._auth.record_budget_overrun(agent_id)
            if self._on_event:
                self._on_event(
                    "budget_overrun",
                    {
                        "task_id": task_id,
                        "agent_id": agent_id,
                        "estimate_cents": estimate,
                        "actual_cents": actual_cents,
                    },
                )

    async def run_mission(self, goal_text: str, job_revenue_cents: int | None = None) -> TaskPlan:
        """
        Execute the mission pipeline: plan -> fiscal clearance (budget + profitability) -> Strategic Intent.

        1. CEO (Strategist) produces a TaskPlan.
        2. For each task, CFO (Treasury) approves budget; on first denial, aborts with FiscalInsolvencyError.
        3. If job_revenue_cents is set and Charter has min_job_margin_ratio, CFO checks unit economics
           (estimated cost must not exceed revenue * (1 - min_margin)); else UnprofitableJobError.
        4. If all cleared, log Strategic Intent and return the plan for Agent Dispatch (Phase 3).
        """
        try:
            from sovereign_os.telemetry.tracer import span_governance
        except ImportError:
            span_governance = lambda **kw: __import__("contextlib").contextmanager(lambda: (yield))()
        with span_governance("run_mission", goal_preview=(goal_text[:80] + "..." if len(goal_text) > 80 else goal_text)):
            logger.info("GOVERNANCE: Mission started. Goal: %s", (goal_text[:200] + "..." if len(goal_text) > 200 else goal_text))
            plan = await self._strategist.create_plan(goal_text)

            total_estimated_cents = 0
            for task in plan.tasks:
                estimated_cents = self._cost_converter(task)
                total_estimated_cents += estimated_cents
                self._task_estimate_cents[task.task_id] = estimated_cents
                try:
                    self._treasury.approve_task(
                        estimated_cents,
                        task_id=task.task_id,
                        purpose=task.description[:100] or "task",
                        skill=task.required_skill,
                    )
                except FiscalInsolvencyError as e:
                    logger.error(
                        "GOVERNANCE: Mission aborted — CFO denied budget for task %s. %s",
                        task.task_id,
                        str(e),
                    )
                    raise

            if job_revenue_cents is not None and job_revenue_cents > 0:
                try:
                    self._treasury.approve_job_profitability(job_revenue_cents, total_estimated_cents)
                except UnprofitableJobError as e:
                    logger.error(
                        "GOVERNANCE: Mission aborted — CFO rejected unprofitable job. %s",
                        str(e),
                    )
                    raise

            logger.info(
                "GOVERNANCE: Strategic Intent logged. %d tasks approved; ready for Agent Dispatch (Phase 3).",
                len(plan.tasks),
            )
            balance_cents = self._ledger.total_usd_cents()
            if self._on_event:
                self._on_event("plan_created", {"goal": goal_text, "tasks": [{"task_id": t.task_id, "required_skill": t.required_skill} for t in plan.tasks]})
                self._on_event(
                    "cfo_approved",
                    {
                        "task_count": len(plan.tasks),
                        "estimated_cents": total_estimated_cents,
                        "balance_cents": balance_cents,
                    },
                )
            return plan

    def _default_cost_converter(self, task: PlannedTask) -> int:
        """
        Pre-flight cost estimate via per-model pricing.

        The CFO budgets on the same basis the ledger later records actuals (the
        model Treasury would pick for this task's priority), so over/under-budget
        is meaningful instead of a flat ~20x over-estimate.
        """
        from sovereign_os.governance.pricing import estimate_budget_cost_cents, output_ratio_for_skill

        model_id = self._treasury.get_optimal_model(getattr(task, "priority", "low"))
        budget = getattr(task, "estimated_token_budget", 2000) or 2000
        ratio = output_ratio_for_skill(getattr(task, "required_skill", ""))
        return estimate_budget_cost_cents(model_id, budget, output_ratio=ratio)

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

    async def _run_auction(self, plan: TaskPlan) -> dict[str, str]:
        """Run RFP auction for each task; return task_id -> winner_agent_id. Skips when no bidding engine."""
        if self._bidding_engine is None:
            return {}
        winner_by_task: dict[str, str] = {}
        runway_cents = self._ledger.total_usd_cents()
        for task in plan.tasks:
            rfp = RequestForProposal(
                task_id=task.task_id,
                description=task.description,
                required_skill=task.required_skill,
                estimated_token_budget=task.estimated_token_budget,
                priority=task.priority,
            )
            bids = await self._bidding_engine.broadcast_rfp(rfp)
            winner = self._treasury.select_winner(bids, task_priority=task.priority, auth=self._auth)
            if winner is not None:
                winner = self._treasury.negotiate(winner, runway_cents)
                winner_by_task[task.task_id] = winner.agent_id
                runway_cents -= winner.estimated_cost_cents
            else:
                winner_by_task[task.task_id] = f"{task.required_skill}-{task.task_id}"
            if self._on_event:
                self._on_event("auction_winner", {"task_id": task.task_id, "winner_agent_id": winner_by_task[task.task_id]})
        return winner_by_task

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
        winner_by_task_id: dict[str, str] | None = None,
    ) -> None:
        """Execute a single task: auth check, rate limit, worker.execute, then record result and lifecycle."""
        agent_id = (winner_by_task_id or {}).get(task.task_id) or f"{task.required_skill}-{task.task_id}"
        capability = self._required_capability_for_skill(task.required_skill)
        if not self._auth.check_permission(agent_id, capability):
            score = self._auth.get_trust_score(agent_id)
            threshold = self._auth.get_threshold(capability)
            if self._on_event:
                self._on_event("permission_denied", {
                    "task_id": task.task_id,
                    "agent_id": agent_id,
                    "skill": task.required_skill,
                    "capability": capability.value,
                    "score": score,
                    "threshold": threshold,
                })
            lifecycle.set_failed(task.task_id, agent_id=agent_id, error="permission_denied")
            raise PermissionDeniedError(
                agent_id,
                capability,
                self._auth.get_trust_score(agent_id),
                self._auth.get_threshold(capability),
            )
        # Graduated spend ceiling: for tasks that actually spend USD, the boolean
        # SPEND_USD grant is necessary but not sufficient — the estimated cost must
        # also fit the agent's trust-scaled ceiling. Ordinary token-burning tasks
        # (research/code/etc.) are governed by the CFO treasury budget, not this.
        if capability == Capability.SPEND_USD:
            spend_cents = self._task_estimate_cents.get(task.task_id) or self._cost_converter(task)
            if not self._auth.can_spend(agent_id, spend_cents):
                ceiling = self._auth.max_spend_cents_for(agent_id)
                logger.warning(
                    "GOVERNANCE: Agent [%s] spend %d cents exceeds graduated ceiling %d cents (task %s).",
                    agent_id, spend_cents, ceiling, task.task_id,
                )
                if self._on_event:
                    self._on_event("spend_limit_exceeded", {
                        "task_id": task.task_id,
                        "agent_id": agent_id,
                        "requested_cents": spend_cents,
                        "ceiling_cents": ceiling,
                    })
                lifecycle.set_failed(task.task_id, agent_id=agent_id, error="spend_limit_exceeded")
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
            self._on_event("task_started", {"task_id": task.task_id, "agent_id": agent_id, "skill": task.required_skill})
        worker = self._registry.get_worker(
            task.required_skill,
            agent_id,
            task_description=task.description,
            memory_manager=self._memory_manager,
        )
        tool_names = get_tools_for_skill(task.required_skill)
        ctx: dict[str, Any] = {
            "goal_summary": task_plan.goal_summary,
            "mcp_tool_names": ",".join(tool_names) if isinstance(tool_names, list) else str(tool_names),
        }
        if getattr(task_plan, "original_goal", None):
            ctx["original_goal"] = task_plan.original_goal
        task_input = TaskInput(
            task_id=task.task_id,
            description=task.description,
            required_skill=task.required_skill,
            context=ctx,
        )
        try:
            result = await worker.execute(task_input)
            result_by_id[task.task_id] = result
            # Record token cost whenever usage is reported — including failed tasks, which
            # still burn tokens. (Gating on success previously leaked the cost of failures.)
            meta = result.metadata or {}
            has_usage = meta.get("input_tokens") is not None and meta.get("output_tokens") is not None
            if self._ledger and hasattr(self._ledger, "record_token") and (result.success or has_usage):
                from sovereign_os.governance.pricing import estimate_cost_cents

                inp = meta.get("input_tokens")
                out = meta.get("output_tokens")
                model_id = meta.get("model_id") or getattr(getattr(worker, "llm", None), "model_name", None) or "default"
                if inp is None or out is None:
                    bud = getattr(task, "estimated_token_budget", 2000)
                    inp, out = bud // 2, bud - bud // 2
                    est_cents = self._cost_converter(task)
                else:
                    inp, out = int(inp), int(out)
                    # Accurate per-model cost (input/output priced separately).
                    est_cents = estimate_cost_cents(model_id, inp, out)
                goal_abbr = (task_plan.goal_summary or "")[:36].strip()
                if goal_abbr:
                    goal_abbr = goal_abbr.replace("\n", " ").strip()
                agent_display = (agent_id or "").split("-")[0].strip() if (agent_id and "-" in agent_id) else (agent_id or "")
                self._ledger.record_token(
                    model_id=model_id or "default",
                    input_tokens=inp,
                    output_tokens=out,
                    agent_id=agent_display or agent_id,
                    task_id=task.task_id,
                    task_display=goal_abbr,
                    estimated_usd_cents=est_cents,
                )
                self._reconcile_cost(task.task_id, agent_id, est_cents)
                self._mission_spent_cents += est_cents
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

    async def dispatch(self, task_plan: TaskPlan, winner_by_task_id: dict[str, str] | None = None) -> list[TaskResult]:
        """
        DAG-aware dispatch: tasks with no remaining dependencies run concurrently via asyncio.gather.
        When winner_by_task_id is provided (from auction), each task is assigned to the winning agent.
        TaskLifecycleManager tracks PENDING -> RUNNING -> COMPLETED | FAILED; structured JSON logging.
        """
        try:
            from sovereign_os.telemetry.tracer import span_governance
        except ImportError:
            span_governance = lambda **kw: __import__("contextlib").contextmanager(lambda: (yield))()
        with span_governance("dispatch", task_count=len(task_plan.tasks)):
            task_by_id = {t.task_id: t for t in task_plan.tasks}
            lifecycle = TaskLifecycleManager([t.task_id for t in task_plan.tasks])
            result_by_id: dict[str, TaskResult] = {}
            wbt = winner_by_task_id or {}
            self._mission_spent_cents = 0  # reset cumulative spend for this dispatch run

            while not lifecycle.all_done():
                cap = self._mission_cost_cap_cents()
                if cap > 0 and self._mission_spent_cents >= cap:
                    self._halt_remaining_for_budget(task_plan, lifecycle, result_by_id, cap)
                    break
                completed = lifecycle.completed_ids()
                ready_ids = self._ready_task_ids(task_plan, completed)
                if not ready_ids:
                    if not lifecycle.all_done():
                        _log_task_transition("_dag", "stall", completed=list(completed), snapshot=lifecycle.snapshot())
                    break
                tasks_to_run = [task_by_id[tid] for tid in ready_ids]
                coros = [
                    self._run_one_task(task, task_plan, lifecycle, result_by_id, winner_by_task_id=wbt)
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
        job_revenue_cents: int | None = None,
    ) -> tuple[TaskPlan, list[TaskResult], list[AuditReport]]:
        """
        Full pipeline: run_mission (with optional profitability check) -> dispatch -> audit each result.
        On audit pass: SovereignAuth.record_audit_success(agent_id).
        On audit fail: SovereignAuth.record_audit_failure(agent_id); optionally abort (raise AuditFailureError).
        When job_revenue_cents is set, CFO enforces min_job_margin_ratio (unit economics).
        """
        plan = await self.run_mission(goal_text, job_revenue_cents=job_revenue_cents)
        plan.original_goal = goal_text  # So workers receive full client brief for industrial delivery
        winner_by_task_id = await self._run_auction(plan)
        results = await self.dispatch(plan, winner_by_task_id=winner_by_task_id)
        reports: list[AuditReport] = []

        if self._review_engine is None:
            logger.warning("GOVERNANCE: No ReviewEngine configured; skipping audit.")
            return plan, results, reports

        try:
            from sovereign_os.telemetry.tracer import record_mission_success
        except ImportError:
            record_mission_success = lambda *a, **k: None
        # Value-aware quality bar: higher-paid jobs are held to a stricter audit score.
        from sovereign_os.auditor.review_engine import value_aware_min_score

        job_min_score = value_aware_min_score(job_revenue_cents)
        for task, result in zip(plan.tasks, results, strict=True):
            report = await self._review_engine.audit_task(task, result, min_score=job_min_score)
            reports.append(report)
            agent_id = winner_by_task_id.get(task.task_id) or f"{task.required_skill}-{task.task_id}"
            judge_model = getattr(self._review_engine, "judge_model", "audit")
            # Quality-scaled trust: a strong pass earns more than a marginal one.
            # Also accrue per-category (delivery-domain) trust from the task's skill.
            from sovereign_os.agents.categories import category_for_skill

            self._auth.record_audit(
                agent_id, passed=report.passed, score=report.score,
                category=category_for_skill(task.required_skill).key,
            )
            if report.passed:
                record_mission_success(judge_model, True)
                if self._memory_manager is not None:
                    self._memory_manager.add_success(
                        task_id=task.task_id,
                        agent_id=agent_id,
                        audit_score=report.score,
                        kpi_target=report.kpi_name,
                        raw_output=result.output,
                        lessons_learned="Task verified against KPI.",
                    )
                logger.info(
                    "AUDIT: Task [%s] verified against KPI [%s]. Quality Score: %.2f.",
                    task.task_id,
                    report.kpi_name,
                    report.score,
                )
            else:
                record_mission_success(judge_model, False)
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
