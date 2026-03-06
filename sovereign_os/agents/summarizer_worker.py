"""
SummarizerWorker: Real worker that uses an LLM to summarize the task description.
Example of a non-stub worker for docs and tests.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sovereign_os.agents.base import BaseWorker, TaskInput, TaskResult

if TYPE_CHECKING:
    from sovereign_os.governance.auction import Bid, RequestForProposal

logger = logging.getLogger(__name__)


class SummarizerWorker(BaseWorker):
    """
    Worker that calls an LLM to produce a short summary of the task description.
    Requires self.llm (ChatLLM) to be set by the registry; otherwise returns a fallback string.
    """

    async def execute(self, task: TaskInput) -> TaskResult:
        desc = (task.description or "").strip() or task.task_id
        if not self.llm:
            return TaskResult(
                task_id=task.task_id,
                success=True,
                output=f"[SummarizerWorker] No LLM; echo: {desc[:200]}",
                metadata={"worker": "SummarizerWorker"},
            )
        prompt = f"Summarize the following in 1–3 concise sentences. Task: {desc}"
        try:
            system = (self.system_prompt or "You are a concise summarizer.").strip() or "You are a concise summarizer."
            messages = [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt or "Summarize."},
            ]
            content = await self.llm.chat(messages)
            output = (content or "").strip() or "[No summary produced]"
            return TaskResult(
                task_id=task.task_id,
                success=True,
                output=output[:65536],
                metadata={"worker": "SummarizerWorker"},
            )
        except Exception as e:
            logger.exception("SummarizerWorker execute failed: %s", e)
            return TaskResult(
                task_id=task.task_id,
                success=False,
                output=f"[SummarizerWorker] Error: {e}",
                metadata={"worker": "SummarizerWorker", "error": str(e)},
            )

    async def get_bid(self, rfp: "RequestForProposal") -> "Bid | None":
        """Optional: bid for auction (modest cost and confidence)."""
        try:
            from sovereign_os.governance.auction import Bid
            return Bid(
                agent_id=self.agent_id,
                estimated_cost_cents=max(1, (rfp.estimated_token_budget * 15) // 1000),
                estimated_time_seconds=10.0,
                confidence_score=0.75,
                model_id=getattr(self.llm, "model_name", "summarizer") if self.llm else "summarizer",
            )
        except Exception:
            return None
