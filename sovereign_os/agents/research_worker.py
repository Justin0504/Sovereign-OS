"""
ResearchWorker: Built-in worker that uses an LLM for short research on a topic.
Outputs bullet points and a brief conclusion. Used with default charter skill "research".
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sovereign_os.agents.base import BaseWorker, TaskInput, TaskResult

if TYPE_CHECKING:
    from sovereign_os.governance.auction import Bid, RequestForProposal

logger = logging.getLogger(__name__)


class ResearchWorker(BaseWorker):
    """
    Worker that calls an LLM to produce short research: 3–5 bullet points + one paragraph summary.
    Requires self.llm (ChatLLM) to be set by the registry.
    """

    async def execute(self, task: TaskInput) -> TaskResult:
        desc = (task.description or "").strip() or task.task_id
        if not self.llm:
            return TaskResult(
                task_id=task.task_id,
                success=True,
                output=f"[ResearchWorker] No LLM; echo: {desc[:200]}",
                metadata={"worker": "ResearchWorker"},
            )
        prompt = (
            "Based on the following topic or question, provide:\n"
            "1. Three to five bullet points (key facts or findings).\n"
            "2. One short paragraph (2–4 sentences) as a conclusion.\n\n"
            f"Topic/Question: {desc}"
        )
        try:
            system = (
                (self.system_prompt or "You are a concise researcher.").strip()
                or "You are a concise researcher. Be factual and brief."
            )
            messages = [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt or "Research."},
            ]
            content = await self.llm.chat(messages)
            output = (content or "").strip() or "[No research produced]"
            return TaskResult(
                task_id=task.task_id,
                success=True,
                output=output[:65536],
                metadata={"worker": "ResearchWorker"},
            )
        except Exception as e:
            logger.exception("ResearchWorker execute failed: %s", e)
            return TaskResult(
                task_id=task.task_id,
                success=False,
                output=f"[ResearchWorker] Error: {e}",
                metadata={"worker": "ResearchWorker", "error": str(e)},
            )

    async def get_bid(self, rfp: "RequestForProposal") -> "Bid | None":
        try:
            from sovereign_os.governance.auction import Bid
            return Bid(
                agent_id=self.agent_id,
                estimated_cost_cents=max(1, (rfp.estimated_token_budget * 20) // 1000),
                estimated_time_seconds=15.0,
                confidence_score=0.75,
                model_id=getattr(self.llm, "model_name", "research") if self.llm else "research",
            )
        except Exception:
            return None
