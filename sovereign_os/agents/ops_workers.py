"""
Built-in operational workers for common 'agent' jobs:
- extract_structured: extract JSON from text
- spec_writer: write a scope/SOW with acceptance criteria
- info_collect: collect information (without external tools) as plan + summary + questions

These are intentionally conservative: they do not pretend to have browsed the web.
If you enable MCP self-hiring, the CEO can also plan tool-backed tasks that use MCPWorker.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

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


def _full_brief(task: TaskInput) -> str:
    """Primary client brief: original_goal or task description. For industrial delivery."""
    brief = _ctx(task, "original_goal", "").strip()
    if not brief:
        brief = (task.description or "").strip() or (task.task_id or "")
    return brief


_DELIVERABLE_RULES = (
    "Use Markdown with ## for main sections and ### for subsections. "
    "Neutral, scannable style; no marketing language. Do not invent scope or commitments."
)


class ExtractStructuredWorker(BaseWorker):
    """
    Extract structured data as JSON.

    Input:
      - task.description: source text
      - task.context["schema"]: optional JSON schema-like description or sample JSON
    Output: JSON block + short summary.
    """

    async def execute(self, task: TaskInput) -> TaskResult:
        src = (task.description or "").strip() or task.task_id
        schema = _ctx(task, "schema", "")
        if not self.llm:
            return TaskResult(
                task_id=task.task_id,
                success=True,
                output=f"[ExtractStructuredWorker] No LLM; echo: {src[:200]}",
                metadata={"worker": "ExtractStructuredWorker", "deliverable_type": "json"},
            )
        system = (self.system_prompt or "You extract structured data carefully.").strip()
        user = (
            "Extract structured data from the text.\n\n"
            f"Schema (optional):\n{schema or '[not provided]'}\n\n"
            f"Text:\n{src}\n\n"
            "Output STRICTLY as:\n"
            "1) JSON (single object) in a fenced code block ```json\n"
            "2) A short summary of extracted fields and missing fields.\n"
            "Rules: do not invent values; if missing, use null."
        )
        try:
            content = await self.llm.chat(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ]
            )
            out = (content or "").strip() or "[No extraction produced]"
            usage = getattr(self.llm, "_last_usage", None)
            meta = {"worker": "ExtractStructuredWorker", "deliverable_type": "json", "model_id": getattr(self.llm, "model_name", "default")}
            if usage:
                meta["input_tokens"], meta["output_tokens"] = usage.get("input_tokens", 0), usage.get("output_tokens", 0)
            return TaskResult(task_id=task.task_id, success=True, output=out[:65536], metadata=meta)
        except Exception as e:
            logger.exception("ExtractStructuredWorker failed: %s", e)
            return TaskResult(
                task_id=task.task_id,
                success=False,
                output=f"[ExtractStructuredWorker] Error: {e}",
                metadata={"worker": "ExtractStructuredWorker", "error": str(e)},
            )


class SpecWriterWorker(BaseWorker):
    """Write SOW/spec, or reusable template + filled example when the client requests it. Industrial delivery."""

    async def execute(self, task: TaskInput) -> TaskResult:
        brief = _full_brief(task)
        if not self.llm:
            return TaskResult(
                task_id=task.task_id,
                success=True,
                output=f"[SpecWriterWorker] No LLM; echo: {brief[:200]}",
                metadata={"worker": "SpecWriterWorker", "deliverable_type": "markdown"},
            )
        audience = _ctx(task, "audience", "client")
        system = (self.system_prompt or "You write clear scopes, specs, and reusable templates. Follow the client request exactly.").strip()
        user = (
            "Client request (follow exactly):\n\n"
            f"{brief}\n\n"
            f"Audience: {audience}. {_DELIVERABLE_RULES}\n\n"
            "**If the request asks for a reusable template plus a filled example**, use this shape:\n"
            "- ## Template — ### Exec summary (half page), ### Decisions log (bullets + owner column), ### Open questions (with owners), ### Next-meeting agenda\n"
            "- ## Filled example — same subsections with realistic placeholder content (e.g. Q1 goals, headcount, roadmap) so the client sees how to fill the template\n"
            "**If the request is a standard scope/spec**: ## Goal, ## Scope (in/out), ## Deliverables, ## Assumptions, ## Acceptance criteria, ## Risks, ## Open questions (max 7). "
            "Output only the Markdown document, no preamble."
        )
        try:
            content = await self.llm.chat(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ]
            )
            out = (content or "").strip() or "[No spec produced]"
            usage = getattr(self.llm, "_last_usage", None)
            meta = {"worker": "SpecWriterWorker", "deliverable_type": "markdown", "model_id": getattr(self.llm, "model_name", "default")}
            if usage:
                meta["input_tokens"], meta["output_tokens"] = usage.get("input_tokens", 0), usage.get("output_tokens", 0)
            return TaskResult(task_id=task.task_id, success=True, output=out[:65536], metadata=meta)
        except Exception as e:
            logger.exception("SpecWriterWorker failed: %s", e)
            return TaskResult(
                task_id=task.task_id,
                success=False,
                output=f"[SpecWriterWorker] Error: {e}",
                metadata={"worker": "SpecWriterWorker", "error": str(e)},
            )


class InfoCollectorWorker(BaseWorker):
    """
    Collect information WITHOUT external tools: produce a research plan + what is known + what to verify.
    This is Claude-like behavior: helpful, structured, transparent about uncertainty.
    """

    async def execute(self, task: TaskInput) -> TaskResult:
        desc = (task.description or "").strip() or task.task_id
        if not self.llm:
            return TaskResult(
                task_id=task.task_id,
                success=True,
                output=f"[InfoCollectorWorker] No LLM; echo: {desc[:200]}",
                metadata={"worker": "InfoCollectorWorker", "deliverable_type": "markdown"},
            )
        depth = _ctx(task, "depth", "quick")
        output_format = _ctx(task, "format", "brief")
        system = (self.system_prompt or "You collect and organize information responsibly.").strip()
        user = (
            "Help collect information for the request.\n\n"
            f"Request:\n{desc}\n\n"
            f"Depth: {depth} (quick|standard|deep)\nOutput format: {output_format}\n\n"
            "Output in Markdown:\n"
            "- What you understood\n"
            "- Clarifying questions (if needed)\n"
            "- Research plan (what sources to check, what to validate)\n"
            "- Provisional answer/summary (clearly labeled as provisional)\n"
            "- Checklist for verification\n"
            "Rules: do not claim you browsed the web; label uncertain points."
        )
        try:
            content = await self.llm.chat(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ]
            )
            out = (content or "").strip() or "[No info produced]"
            usage = getattr(self.llm, "_last_usage", None)
            meta = {"worker": "InfoCollectorWorker", "deliverable_type": "markdown", "model_id": getattr(self.llm, "model_name", "default")}
            if usage:
                meta["input_tokens"], meta["output_tokens"] = usage.get("input_tokens", 0), usage.get("output_tokens", 0)
            return TaskResult(task_id=task.task_id, success=True, output=out[:65536], metadata=meta)
        except Exception as e:
            logger.exception("InfoCollectorWorker failed: %s", e)
            return TaskResult(
                task_id=task.task_id,
                success=False,
                output=f"[InfoCollectorWorker] Error: {e}",
                metadata={"worker": "InfoCollectorWorker", "error": str(e)},
            )

