"""
Code-related workers: code understanding, suggestions, and review (LLM-only, no execution).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sovereign_os.agents.base import BaseWorker, TaskInput, TaskResult

if TYPE_CHECKING:
    from sovereign_os.governance.auction import Bid, RequestForProposal

logger = logging.getLogger(__name__)


def _ctx(task: TaskInput, key: str, default: str = "") -> str:
    try:
        v = (task.context or {}).get(key, default)
        return str(v).strip()
    except Exception:
        return default


async def _chat(worker: BaseWorker, system: str, user: str) -> str:
    assert worker.llm is not None
    content = await worker.llm.chat(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
    )
    return (content or "").strip()


class CodeAssistantWorker(BaseWorker):
    """Understand code, suggest changes, or explain behavior. Analysis only; does not execute code."""

    async def execute(self, task: TaskInput) -> TaskResult:
        desc = (task.description or "").strip() or task.task_id
        if not self.llm:
            return TaskResult(
                task_id=task.task_id,
                success=True,
                output=f"[CodeAssistantWorker] No LLM; echo: {desc[:200]}",
                metadata={"worker": "CodeAssistantWorker", "deliverable_type": "markdown"},
            )
        code = _ctx(task, "code", "")
        language = _ctx(task, "language", "")
        system = (
            self.system_prompt
            or "You are a code assistant. Analyze and explain code; suggest fixes or improvements. Do not execute code. Output in Markdown."
        ).strip()
        user = (
            f"Request:\n{desc}\n\n"
            + (f"Code ({language}):\n```\n{code}\n```\n" if code else "No code snippet provided.")
        )
        try:
            out = await _chat(self, system, user)
            return TaskResult(
                task_id=task.task_id,
                success=True,
                output=(out or "[No analysis]")[:65536],
                metadata={"worker": "CodeAssistantWorker", "deliverable_type": "markdown"},
            )
        except Exception as e:
            logger.exception("CodeAssistantWorker failed: %s", e)
            return TaskResult(
                task_id=task.task_id,
                success=False,
                output=f"[CodeAssistantWorker] Error: {e}",
                metadata={"worker": "CodeAssistantWorker", "error": str(e)},
            )

    async def get_bid(self, rfp: "RequestForProposal") -> "Bid | None":
        try:
            from sovereign_os.governance.auction import Bid

            return Bid(
                agent_id=self.agent_id,
                estimated_cost_cents=max(2, (rfp.estimated_token_budget * 25) // 1000),
                estimated_time_seconds=15.0,
                confidence_score=0.75,
                model_id=getattr(self.llm, "model_name", "code") if self.llm else "code",
            )
        except Exception:
            return None


class CodeReviewWorker(BaseWorker):
    """Review code for issues, style, and best practices. Output only; does not execute or modify code."""

    async def execute(self, task: TaskInput) -> TaskResult:
        desc = (task.description or "").strip() or task.task_id
        if not self.llm:
            return TaskResult(
                task_id=task.task_id,
                success=True,
                output=f"[CodeReviewWorker] No LLM; echo: {desc[:200]}",
                metadata={"worker": "CodeReviewWorker", "deliverable_type": "markdown"},
            )
        code = _ctx(task, "code", "")
        language = _ctx(task, "language", "")
        system = (
            self.system_prompt
            or "You are a code reviewer. Review the code for bugs, style, security, and maintainability. Do not execute or modify code. Output in Markdown: summary, list of issues (with severity), and suggested improvements."
        ).strip()
        user = (
            f"Review request:\n{desc}\n\n"
            + (f"Code ({language}):\n```\n{code}\n```" if code else "No code provided.")
        )
        try:
            out = await _chat(self, system, user)
            return TaskResult(
                task_id=task.task_id,
                success=True,
                output=(out or "[No review]")[:65536],
                metadata={"worker": "CodeReviewWorker", "deliverable_type": "markdown"},
            )
        except Exception as e:
            logger.exception("CodeReviewWorker failed: %s", e)
            return TaskResult(
                task_id=task.task_id,
                success=False,
                output=f"[CodeReviewWorker] Error: {e}",
                metadata={"worker": "CodeReviewWorker", "error": str(e)},
            )

    async def get_bid(self, rfp: "RequestForProposal") -> "Bid | None":
        try:
            from sovereign_os.governance.auction import Bid

            return Bid(
                agent_id=self.agent_id,
                estimated_cost_cents=max(2, (rfp.estimated_token_budget * 30) // 1000),
                estimated_time_seconds=20.0,
                confidence_score=0.7,
                model_id=getattr(self.llm, "model_name", "review") if self.llm else "review",
            )
        except Exception:
            return None
