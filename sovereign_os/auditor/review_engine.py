"""
ReviewEngine: Orchestrates task review — KPI resolution, Judge LLM, AuditReport.
Post-audit reflection: on failure, generates ReflectionObject and persists to Memory (high priority).
"""

import json
import logging
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from sovereign_os.memory.manager import MemoryManager

from sovereign_os.agents.base import TaskResult
from sovereign_os.auditor.base import AuditReport, BaseAuditor
from sovereign_os.auditor.kpi_validator import KPIValidator
from sovereign_os.governance.strategist import PlannedTask
from sovereign_os.llm.providers import ChatLLM, create_llm_client
from sovereign_os.memory.schema import ReflectionObject
from sovereign_os.models.charter import Charter

logger = logging.getLogger(__name__)

# Reasoning-heavy default for high-precision judgment (used as logical role only)
DEFAULT_JUDGE_MODEL = "gpt-4o"


class JudgeLLMProtocol(Protocol):
    """Async interface for Judge LLM: returns passed, score, reason, suggested_fix."""

    async def evaluate(
        self,
        task_id: str,
        task_output: str,
        verification_prompt: str,
        kpi_name: str,
    ) -> AuditReport:
        ...


class JudgeLLM(BaseAuditor):
    """
    Judge LLM: calls reasoning model to return JSON
    { "passed": bool, "score": float, "reason": str, "suggested_fix": str }.
    """

    def __init__(self, *, client: ChatLLM | None = None) -> None:
        self._client = client or create_llm_client("judge")
        self._model = getattr(self._client, "model_name", DEFAULT_JUDGE_MODEL)

    async def evaluate(
        self,
        task_id: str,
        task_output: str,
        verification_prompt: str,
        kpi_name: str,
    ) -> AuditReport:
        system = (
            "You are an auditor. Given a task output and a verification question, respond with ONLY a JSON object "
            'with keys: "passed" (bool), "score" (float 0-1), "reason" (str), "suggested_fix" (str). '
            "No markdown, no explanation outside JSON."
        )
        user = (
            f"KPI: {kpi_name}\nVerification: {verification_prompt}\n\nTask output:\n{task_output[:4000]}\n\nJSON:"
        )
        try:
            from sovereign_os.telemetry.tracer import span_llm
        except ImportError:
            span_llm = lambda *a, **kw: __import__("contextlib").contextmanager(lambda: (yield))()
        with span_llm("judge.evaluate", model=self._model, task_id=task_id, kpi_name=kpi_name):
            content = await self._client.chat(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ]
            )
        content = (content or "{}").strip()
        content = content.removeprefix("```json").removeprefix("```").strip().removesuffix("```").strip()
        data = json.loads(content)
        return AuditReport(
            task_id=task_id,
            kpi_name=kpi_name,
            passed=bool(data.get("passed", False)),
            score=float(data.get("score", 0.0)),
            reason=str(data.get("reason", "")),
            suggested_fix=str(data.get("suggested_fix", "")),
        )


class StubAuditor(BaseAuditor):
    """Rule-based fallback when no Judge LLM: pass if task_result.success and non-empty output."""

    async def evaluate(
        self,
        task_id: str,
        task_output: str,
        verification_prompt: str,
        kpi_name: str,
    ) -> AuditReport:
        passed = bool(task_output.strip())  # Stub: any non-empty output passes
        return AuditReport(
            task_id=task_id,
            kpi_name=kpi_name or "default",
            passed=passed,
            score=0.9 if passed else 0.0,
            reason="Stub verification: output present" if passed else "Stub: empty output",
            suggested_fix="" if passed else "Provide non-empty task output.",
        )


class ReviewEngine:
    """
    Main audit orchestrator: resolve KPI, run Judge, return AuditReport.
    When audit fails: generates ReflectionObject and persists to MemoryManager (high priority).
    """

    def __init__(
        self,
        charter: Charter,
        *,
        judge: BaseAuditor | None = None,
        memory_manager: "MemoryManager | None" = None,
    ) -> None:
        self._charter = charter
        self._kpi = KPIValidator(charter)
        if judge is not None:
            self._judge = judge
        else:
            try:
                self._judge = JudgeLLM()
            except Exception as e:  # pragma: no cover - optional LLM path
                logger.warning(
                    "GOVERNANCE AUDITOR: No Judge LLM configured; "
                    "falling back to StubAuditor. (%s)",
                    e,
                )
                self._judge = StubAuditor()
        self._memory = memory_manager

    @property
    def judge_model(self) -> str:
        """Model ID used by the Judge (for metrics)."""
        return getattr(self._judge, "_model", "stub")

    async def audit_task(self, task_plan_item: PlannedTask, task_result: TaskResult) -> AuditReport:
        """
        1. Identify KPI from Charter.success_kpis for this task.
        2. Build verification prompt from KPI.
        3. Run Judge (LLM or stub) → JSON → AuditReport.
        4. If failed and MemoryManager set: persist ReflectionObject (high priority).
        """
        kpi_name, verification_prompt = self._kpi.get_verification_prompt(
            task_plan_item.description,
            task_plan_item.required_skill,
        )
        logger.info(
            "GOVERNANCE AUDITOR: Reviewing output for Task [%s]...",
            task_plan_item.task_id,
        )
        report = await self._judge.evaluate(
            task_id=task_plan_item.task_id,
            task_output=task_result.output,
            verification_prompt=verification_prompt,
            kpi_name=kpi_name,
        )
        outcome = "PASS" if report.passed else "FAIL"
        logger.info(
            "GOVERNANCE AUDITOR: Reviewing output for Task [%s]... [%s]",
            task_plan_item.task_id,
            outcome,
        )
        if not report.passed and self._memory is not None:
            reflection = ReflectionObject(
                failure_reason=report.reason,
                corrected_logic=report.suggested_fix or "Review task requirements and retry with corrected approach.",
                task_id=task_plan_item.task_id,
                agent_id=f"{task_plan_item.required_skill}-{task_plan_item.task_id}",
                kpi_name=kpi_name,
                audit_score=report.score,
                raw_output=task_result.output,
            )
            self._memory.add_reflection(reflection)
        return report
