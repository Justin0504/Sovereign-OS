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
            "Use clean Markdown: ## for main sections, ### for subsections, **bold** for key terms. "
            "Do not invent data — all figures must be public information; label estimates as 'estimated' or 'circa'. "
            "Open with the most important finding. Be specific and scannable.\n\n"
            "**For competitive landscape / comparison requests**, use:\n"
            "- ## Executive Summary — 2–3 sentences on the market and key insight\n"
            "- ## Market Overview — size, trends, and key dynamics (bullets)\n"
            "- ## Competitive Comparison — Markdown table with relevant columns; include a 'Verdict' column\n"
            "- ## Key Differentiators — 3–5 specific, actionable differentiators\n"
            "- ## Recommendations — 2–4 strategic bullets\n\n"
            "**For general research requests**, use:\n"
            "- ## Summary — 3–5 bullet findings (most important first)\n"
            "- ## Deep Dive — supporting analysis with examples\n"
            "- ## Conclusion — one clear paragraph\n"
            "- ## Actionable Next Steps — 3 concrete steps based on the research\n\n"
            "Output only the Markdown document."
        )
        try:
            system = (
                (self.system_prompt or (
                    "You are a senior research analyst. "
                    "You synthesize public information into clear, actionable intelligence. "
                    "Your output should be slide-ready and executive-facing: no filler, maximum insight. "
                    "Always cite the type of source when referencing data (e.g. 'industry report', 'company announcement', 'analyst estimate')."
                )).strip()
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
