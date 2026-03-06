"""
ReplyWorker: Built-in worker that fills a reply template with variables.
Supports simple {{key}} placeholders; optional LLM polish. Used with default charter skill "reply".
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from sovereign_os.agents.base import BaseWorker, TaskInput, TaskResult

if TYPE_CHECKING:
    from sovereign_os.governance.auction import Bid, RequestForProposal

logger = logging.getLogger(__name__)

# Simple {{variable}} placeholder pattern
_PLACEHOLDER = re.compile(r"\{\{\s*(\w+)\s*\}\}")


class ReplyWorker(BaseWorker):
    """
    Worker that fills a reply from a template. Description can be:
    - "Template: Hello {{name}}, your order {{order_id}} is ready. | name=Alice | order_id=123"
    Or just a template line with | key=value pairs. If LLM is set, can optionally polish the result.
    """

    async def execute(self, task: TaskInput) -> TaskResult:
        desc = (task.description or "").strip() or task.task_id
        # Parse template and variables: "Template: ... | k1=v1 | k2=v2" or use task.context
        template = desc
        variables: dict[str, str] = dict(task.context or {})
        if "|" in desc:
            parts = [p.strip() for p in desc.split("|")]
            if parts:
                template = parts[0].removeprefix("Template:").strip() or parts[0]
                for p in parts[1:]:
                    if "=" in p:
                        k, _, v = p.partition("=")
                        variables[k.strip()] = v.strip()
        # Fill placeholders
        def repl(match: re.Match[str]) -> str:
            return variables.get(match.group(1), match.group(0))
        filled = _PLACEHOLDER.sub(repl, template)
        # Optional LLM polish (one sentence only if no placeholders were found and LLM available)
        if self.llm and not _PLACEHOLDER.search(template) and len(filled) < 500:
            try:
                system = (self.system_prompt or "You are a professional reply writer.").strip() or "Be concise and polite."
                content = await self.llm.chat([
                    {"role": "system", "content": system},
                    {"role": "user", "content": f"Turn this into one short, professional reply (1–3 sentences):\n{filled}"},
                ])
                if content and content.strip():
                    filled = content.strip()[:4096]
            except Exception as e:
                logger.debug("ReplyWorker LLM polish failed, using filled template: %s", e)
        return TaskResult(
            task_id=task.task_id,
            success=True,
            output=filled[:65536],
            metadata={"worker": "ReplyWorker"},
        )

    async def get_bid(self, rfp: "RequestForProposal") -> "Bid | None":
        try:
            from sovereign_os.governance.auction import Bid
            cost = 5
            if self.llm:
                cost = max(5, (rfp.estimated_token_budget * 5) // 1000)
            return Bid(
                agent_id=self.agent_id,
                estimated_cost_cents=cost,
                estimated_time_seconds=3.0,
                confidence_score=0.9,
                model_id=getattr(self.llm, "model_name", "reply") if self.llm else "reply",
            )
        except Exception:
            return None
