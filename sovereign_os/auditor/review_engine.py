"""
ReviewEngine: Orchestrates task review — KPI resolution, Judge LLM, AuditReport.
"""

import json
import logging
from typing import Protocol

from sovereign_os.agents.base import TaskResult
from sovereign_os.auditor.base import AuditReport, BaseAuditor
from sovereign_os.auditor.kpi_validator import KPIValidator
from sovereign_os.governance.strategist import PlannedTask
from sovereign_os.models.charter import Charter

logger = logging.getLogger(__name__)

try:
    from openai import AsyncOpenAI
except ImportError:
    AsyncOpenAI = None  # type: ignore[misc, assignment]

# Reasoning-heavy default for high-precision judgment
DEFAULT_JUDGE_MODEL = "gpt-4o"  # or "o1-mini" / "claude-3-5-sonnet" when available


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

    def __init__(self, *, api_key: str | None = None, model: str = DEFAULT_JUDGE_MODEL) -> None:
        if AsyncOpenAI is None:
            raise ImportError("openai package required for JudgeLLM; pip install openai")
        self._client = AsyncOpenAI(api_key=api_key)
        self._model = model

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
        response = await self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        content = (response.choices[0].message.content or "{}").strip()
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
    """

    def __init__(self, charter: Charter, *, judge: BaseAuditor | None = None) -> None:
        self._charter = charter
        self._kpi = KPIValidator(charter)
        self._judge = judge or StubAuditor()

    async def audit_task(self, task_plan_item: PlannedTask, task_result: TaskResult) -> AuditReport:
        """
        1. Identify KPI from Charter.success_kpis for this task.
        2. Build verification prompt from KPI.
        3. Run Judge (LLM or stub) → JSON → AuditReport.
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
        return report
