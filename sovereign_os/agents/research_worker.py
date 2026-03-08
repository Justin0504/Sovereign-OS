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


def _full_brief(task: TaskInput) -> str:
    """Primary client brief: original_goal or task description. For industrial delivery."""
    try:
        brief = (task.context or {}).get("original_goal", "") or ""
        brief = str(brief).strip()
    except Exception:
        brief = ""
    if not brief:
        brief = (task.description or "").strip() or (task.task_id or "")
    return brief


class ResearchWorker(BaseWorker):
    """
    Worker that produces research: bullets + conclusion, or comparison tables + differentiators when requested.
    Follows full client brief for industrial delivery.
    """

    async def execute(self, task: TaskInput) -> TaskResult:
        brief = _full_brief(task)
        if not self.llm:
            return TaskResult(
                task_id=task.task_id,
                success=True,
                output=f"[ResearchWorker] No LLM; echo: {brief[:200]}",
                metadata={"worker": "ResearchWorker"},
            )
        prompt = (
            "Client request (follow exactly):\n\n"
            f"{brief}\n\n"
            "Use Markdown with ## for main sections and ### for subsections. Do not invent data; use only public information—if a figure is estimated, say 'estimated' or cite 'public sources'.\n\n"
            "**If the request asks for competitive landscape, comparison table, or differentiators**, use this output shape:\n"
            "- ## Overview — 2–4 sentences on the space and the client's position\n"
            "- ## Comparison — a Markdown table with columns the client asked for (e.g. Feature, Pricing, Geography); one row per competitor plus the client\n"
            "- ## Differentiators (or ## Recommendations) — 2–4 bullets with short rationale\n"
            "**Otherwise** (short research): ## Key findings (3–5 bullets), ## Conclusion (one short paragraph).\n"
            "Output only the Markdown document, no preamble."
        )
        try:
            system = (
                (self.system_prompt or "You are a factual researcher. Output slide-ready, scannable content; tables in Markdown.").strip()
                or "You are a concise researcher. Be factual and brief."
            )
            messages = [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt or "Research."},
            ]
            content = await self.llm.chat(messages)
            output = (content or "").strip() or "[No research produced]"
            usage = getattr(self.llm, "_last_usage", None)
            meta = {"worker": "ResearchWorker", "model_id": getattr(self.llm, "model_name", "default")}
            if usage:
                meta["input_tokens"] = usage.get("input_tokens", 0)
                meta["output_tokens"] = usage.get("output_tokens", 0)
            return TaskResult(
                task_id=task.task_id,
                success=True,
                output=output[:65536],
                metadata=meta,
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
