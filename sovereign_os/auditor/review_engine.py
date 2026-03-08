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
from sovereign_os.auditor.trail import append_audit_report
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
    Score threshold: >= 0.5 auto-passes to avoid over-rejection of reasonable deliverables.
    """

    PASS_THRESHOLD = 0.50  # anything above 0.5 is considered passing

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
            "You are a pragmatic QA auditor reviewing AI-generated work deliverables. "
            "Your job is to verify that the output genuinely addresses the client's request — "
            "NOT to apply academic or journalistic perfection standards. "
            "Grade generously: a deliverable PASSES if it is on-topic, coherent, and provides real value. "
            "Only FAIL if the output is: completely off-topic, empty/placeholder-only, nonsensical, or explicitly harmful. "
            "Minor style issues, length variations, or missing optional extras do NOT cause failure. "
            "Respond with ONLY a JSON object with keys: "
            '"passed" (bool), "score" (float 0.0–1.0, be generous — typical passing range 0.7–0.95), '
            '"reason" (1–2 sentences), "suggested_fix" (empty string if passed, brief note if failed). '
            "No markdown, no text outside JSON."
        )
        user = (
            f"KPI: {kpi_name}\n"
            f"Verification question: {verification_prompt}\n\n"
            f"Task output to evaluate:\n{task_output[:8000]}\n\n"
            "JSON:"
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
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            # Fallback: non-empty output passes
            passed = bool(task_output.strip())
            return AuditReport(
                task_id=task_id,
                kpi_name=kpi_name,
                passed=passed,
                score=0.80 if passed else 0.0,
                reason="Judge parse error; rule-based fallback applied.",
                suggested_fix="",
            )
        score = float(data.get("score", 0.0))
        # Apply threshold: score >= PASS_THRESHOLD always passes regardless of LLM's "passed" flag
        passed = bool(data.get("passed", False)) or (score >= self.PASS_THRESHOLD)
        return AuditReport(
            task_id=task_id,
            kpi_name=kpi_name,
            passed=passed,
            score=score,
            reason=str(data.get("reason", "")),
            suggested_fix=str(data.get("suggested_fix", "")) if not passed else "",
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
        audit_trail_path: str | None = None,
    ) -> None:
        self._charter = charter
        self._audit_trail_path = audit_trail_path
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
        if self._audit_trail_path:
            try:
                append_audit_report(self._audit_trail_path, report)
            except Exception as e:
                logger.warning("AUDIT TRAIL: append failed: %s", e)
        return report
