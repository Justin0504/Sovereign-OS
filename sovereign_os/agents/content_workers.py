"""
Built-in, general-purpose content workers.

These are "LLM-only" workers designed to be useful out-of-the-box:
- write_article: long-form article / blog draft
- solve_problem: solve a question with steps and final answer
- write_email: customer/support/sales email drafts
- write_post: social posts (X/LinkedIn/WeChat, etc.)
- meeting_minutes: meeting notes -> decisions + action items
- translate: translation with formatting preserved
- rewrite_polish: rewrite/polish with constraints
"""

from __future__ import annotations

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
    """Primary client brief: original_goal (full job spec) or task description. For industrial delivery."""
    brief = _ctx(task, "original_goal", "").strip()
    if not brief:
        brief = (task.description or "").strip() or (task.task_id or "")
    return brief


# Shared rules for professional deliverables.
_DELIVERABLE_RULES = (
    "Use clean Markdown: ## for main sections, ### for subsections, **bold** for key terms. "
    "Do not invent statistics, quotes, or named studies — if data is estimated, say 'estimated' or 'based on public trends'. "
    "Respect the client's requested word counts and tone. "
    "Open with a clear executive summary or key point — do not bury the lead. "
    "End with actionable takeaways or next steps unless the client specifies otherwise."
)

# System prompt shared across writing workers — establishes professional baseline.
_WRITER_SYSTEM = (
    "You are a senior professional writer with experience in B2B content, thought leadership, and client deliverables. "
    "You write with clarity, precision, and a confident voice. "
    "Always follow the client brief exactly. Prioritize substance over length — every sentence must add value. "
    "Format output for immediate use: well-structured, professional, ready to publish or send."
)


async def _chat(worker: BaseWorker, system: str, user: str) -> tuple[str, dict[str, int] | None]:
    """Return (content, usage_dict). usage_dict has input_tokens, output_tokens from API if available."""
    assert worker.llm is not None
    content = await worker.llm.chat(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
    )
    usage = getattr(worker.llm, "_last_usage", None)
    return (content or "").strip(), usage


def _metadata_with_usage(base: dict, usage: dict[str, int] | None, llm: Any) -> dict:
    out = dict(base)
    if usage:
        out["input_tokens"] = usage.get("input_tokens", 0)
        out["output_tokens"] = usage.get("output_tokens", 0)
    out["model_id"] = getattr(llm, "model_name", "default")
    return out


class ArticleWriterWorker(BaseWorker):
    """Write a structured article draft (outline + draft + title). Industrial delivery with fixed section shape."""

    async def execute(self, task: TaskInput) -> TaskResult:
        brief = _full_brief(task)
        if not self.llm:
            return TaskResult(
                task_id=task.task_id,
                success=True,
                output=f"[ArticleWriterWorker] No LLM; echo: {brief[:200]}",
                metadata={"worker": "ArticleWriterWorker", "deliverable_type": "markdown"},
            )
        language = _ctx(task, "language", "English")
        system = (self.system_prompt or _WRITER_SYSTEM).strip()
        user = (
            "Client request (follow exactly):\n\n"
            f"{brief}\n\n"
            f"Language: {language}. {_DELIVERABLE_RULES}\n\n"
            "**Deliverable structure** (use these exact section headings):\n"
            "- ## Title Options — 3 compelling headline choices with a one-line rationale each\n"
            "- ## Executive Summary — 2–3 sentences capturing the core argument\n"
            "- ## Draft — full article body with ### subheadings; match the client's word count exactly\n"
            "- ## Key Takeaways — 4–6 punchy bullets readers can act on immediately\n"
            "Optional additions if requested: ## Meta Description (1–2 SEO sentences), "
            "## Social Captions (3 platform-ready variants), ## Content Checklist (actionable bullets). "
            "Output only the Markdown document."
        )
        try:
            out, usage = await _chat(self, system, user)
            return TaskResult(
                task_id=task.task_id,
                success=True,
                output=(out or "[No article produced]")[:65536],
                metadata=_metadata_with_usage(
                    {"worker": "ArticleWriterWorker", "deliverable_type": "markdown"},
                    usage,
                    self.llm,
                ),
            )
        except Exception as e:
            logger.exception("ArticleWriterWorker failed: %s", e)
            return TaskResult(
                task_id=task.task_id,
                success=False,
                output=f"[ArticleWriterWorker] Error: {e}",
                metadata={"worker": "ArticleWriterWorker", "error": str(e)},
            )

    async def get_bid(self, rfp: "RequestForProposal") -> "Bid | None":
        try:
            from sovereign_os.governance.auction import Bid

            return Bid(
                agent_id=self.agent_id,
                estimated_cost_cents=max(2, (rfp.estimated_token_budget * 25) // 1000),
                estimated_time_seconds=20.0,
                confidence_score=0.75,
                model_id=getattr(self.llm, "model_name", "article") if self.llm else "article",
            )
        except Exception:
            return None


class ProblemSolverWorker(BaseWorker):
    """Solve a problem with steps and final answer. Output shape: ## Understanding, ## Solution, ## Answer."""

    async def execute(self, task: TaskInput) -> TaskResult:
        desc = (task.description or "").strip() or task.task_id
        if not self.llm:
            return TaskResult(
                task_id=task.task_id,
                success=True,
                output=f"[ProblemSolverWorker] No LLM; echo: {desc[:200]}",
                metadata={"worker": "ProblemSolverWorker", "deliverable_type": "markdown"},
            )
        system = (self.system_prompt or (
            "You are an expert consultant and problem solver. "
            "Provide rigorous, actionable analysis. Show your reasoning step-by-step. "
            "Be direct and specific — avoid vague platitudes."
        )).strip()
        user = (
            f"Problem to solve:\n\n{desc}\n\n"
            f"{_DELIVERABLE_RULES}\n\n"
            "**Output shape**: \n"
            "## Problem Statement — restate the core challenge in 1–2 sentences\n"
            "## Analysis — break down key factors, constraints, and considerations\n"
            "## Solution — step-by-step approach with concrete recommendations\n"
            "## Answer — clear final answer or decision\n"
            "## Next Steps — 3–5 immediate actions to implement the solution\n"
            "If information is missing, note assumptions made and give your best-effort solution. "
            "Output only the Markdown document."
        )
        try:
            out, usage = await _chat(self, system, user)
            return TaskResult(
                task_id=task.task_id,
                success=True,
                output=(out or "[No solution produced]")[:65536],
                metadata=_metadata_with_usage(
                    {"worker": "ProblemSolverWorker", "deliverable_type": "markdown"},
                    usage,
                    self.llm,
                ),
            )
        except Exception as e:
            logger.exception("ProblemSolverWorker failed: %s", e)
            return TaskResult(
                task_id=task.task_id,
                success=False,
                output=f"[ProblemSolverWorker] Error: {e}",
                metadata={"worker": "ProblemSolverWorker", "error": str(e)},
            )

    async def get_bid(self, rfp: "RequestForProposal") -> "Bid | None":
        try:
            from sovereign_os.governance.auction import Bid

            return Bid(
                agent_id=self.agent_id,
                estimated_cost_cents=max(1, (rfp.estimated_token_budget * 20) // 1000),
                estimated_time_seconds=12.0,
                confidence_score=0.7,
                model_id=getattr(self.llm, "model_name", "solver") if self.llm else "solver",
            )
        except Exception:
            return None


class EmailWriterWorker(BaseWorker):
    """Draft email(s): single or sequence. Fixed deliverable shape: Subject, Body, CTA; optional Timing section."""

    async def execute(self, task: TaskInput) -> TaskResult:
        brief = _full_brief(task)
        if not self.llm:
            return TaskResult(
                task_id=task.task_id,
                success=True,
                output=f"[EmailWriterWorker] No LLM; echo: {brief[:200]}",
                metadata={"worker": "EmailWriterWorker", "deliverable_type": "markdown"},
            )
        language = _ctx(task, "language", "English")
        system = (self.system_prompt or (
            "You are an expert email copywriter. "
            "You write emails that get opened, read, and acted on. "
            "Every subject line must earn the open; every body must earn the click. "
            "Be direct, personal, and specific — no corporate jargon or empty phrases."
        )).strip()
        user = (
            "Client request (follow exactly):\n\n"
            f"{brief}\n\n"
            f"Language: {language}. {_DELIVERABLE_RULES}\n\n"
            "**Output shape — single email:**\n"
            "## Subject Lines — 3 options (A/B/C) with a one-word descriptor (e.g. 'Direct', 'Curiosity', 'Value')\n"
            "## Email Body — ready-to-send, conversational tone, ≤150 words unless longer requested\n"
            "## CTA — the one specific action you want the reader to take\n"
            "## Why It Works — 2–3 bullets explaining the persuasion strategy\n\n"
            "**Output shape — email sequence:** For each email: ## Email N, ### Subject, ### Body, ### CTA. "
            "Add ## Timing Strategy at the end (when to send each email). "
            "Output only the Markdown document."
        )
        try:
            out, usage = await _chat(self, system, user)
            return TaskResult(
                task_id=task.task_id,
                success=True,
                output=(out or "[No email produced]")[:65536],
                metadata=_metadata_with_usage(
                    {"worker": "EmailWriterWorker", "deliverable_type": "markdown"},
                    usage,
                    self.llm,
                ),
            )
        except Exception as e:
            logger.exception("EmailWriterWorker failed: %s", e)
            return TaskResult(
                task_id=task.task_id,
                success=False,
                output=f"[EmailWriterWorker] Error: {e}",
                metadata={"worker": "EmailWriterWorker", "error": str(e)},
            )

    async def get_bid(self, rfp: "RequestForProposal") -> "Bid | None":
        try:
            from sovereign_os.governance.auction import Bid

            return Bid(
                agent_id=self.agent_id,
                estimated_cost_cents=max(1, (rfp.estimated_token_budget * 12) // 1000),
                estimated_time_seconds=8.0,
                confidence_score=0.8,
                model_id=getattr(self.llm, "model_name", "email") if self.llm else "email",
            )
        except Exception:
            return None


class SocialPostWorker(BaseWorker):
    """Draft short social posts. Output shape: ## Variations, ## Hashtags, ## CTA."""

    async def execute(self, task: TaskInput) -> TaskResult:
        brief = _full_brief(task)
        if not self.llm:
            return TaskResult(
                task_id=task.task_id,
                success=True,
                output=f"[SocialPostWorker] No LLM; echo: {brief[:200]}",
                metadata={"worker": "SocialPostWorker", "deliverable_type": "markdown"},
            )
        platform = _ctx(task, "platform", "X")
        audience = _ctx(task, "audience", "general audience")
        tone = _ctx(task, "tone", "clear, confident")
        language = _ctx(task, "language", "English")
        system = (self.system_prompt or (
            "You are a social media strategist and copywriter. "
            "You write posts that stop the scroll. "
            "Every post must open with a hook, deliver value, and drive engagement. "
            "Match the platform's culture and character limits precisely."
        )).strip()
        char_limits = {"x": "≤280 chars", "twitter": "≤280 chars", "linkedin": "≤3000 chars, use line breaks", "instagram": "≤2200 chars"}
        char_note = char_limits.get(platform.lower(), "appropriate length for platform")
        user = (
            f"Client request or topic:\n\n{brief}\n\n"
            f"Platform: {platform} ({char_note}). Audience: {audience}. Tone: {tone}. Language: {language}.\n\n"
            "**Output shape**:\n"
            "## Post Variants — 5 distinct versions (label A–E, vary hook style: question/stat/story/bold claim/insight)\n"
            "## Hashtags — 5–8 relevant hashtags with brief rationale\n"
            "## Best Pick — recommend one variant and explain why in 1–2 sentences\n"
            "## Engagement Tip — one tactical suggestion to boost reach (timing, format, reply strategy)\n"
            "Output only the Markdown document."
        )
        try:
            out, usage = await _chat(self, system, user)
            return TaskResult(
                task_id=task.task_id,
                success=True,
                output=(out or "[No post produced]")[:65536],
                metadata=_metadata_with_usage(
                    {"worker": "SocialPostWorker", "deliverable_type": "markdown", "platform": platform},
                    usage,
                    self.llm,
                ),
            )
        except Exception as e:
            logger.exception("SocialPostWorker failed: %s", e)
            return TaskResult(
                task_id=task.task_id,
                success=False,
                output=f"[SocialPostWorker] Error: {e}",
                metadata={"worker": "SocialPostWorker", "error": str(e)},
            )

    async def get_bid(self, rfp: "RequestForProposal") -> "Bid | None":
        try:
            from sovereign_os.governance.auction import Bid

            return Bid(
                agent_id=self.agent_id,
                estimated_cost_cents=max(1, (rfp.estimated_token_budget * 10) // 1000),
                estimated_time_seconds=6.0,
                confidence_score=0.8,
                model_id=getattr(self.llm, "model_name", "post") if self.llm else "post",
            )
        except Exception:
            return None


class HelpContentWorker(BaseWorker):
    """Write help center / product docs. Fixed shape: ## FAQ (Q/A), ## Tooltips (screen | text), ## Getting started."""

    async def execute(self, task: TaskInput) -> TaskResult:
        brief = _full_brief(task)
        if not self.llm:
            return TaskResult(
                task_id=task.task_id,
                success=True,
                output=f"[HelpContentWorker] No LLM; echo: {brief[:200]}",
                metadata={"worker": "HelpContentWorker", "deliverable_type": "markdown"},
            )
        language = _ctx(task, "language", "English")
        system = (self.system_prompt or "You write clear, jargon-free help content. One question per FAQ; tooltips under 15 words unless client says otherwise.").strip()
        user = (
            "Client request (follow exactly):\n\n"
            f"{brief}\n\n"
            f"Language: {language}. {_DELIVERABLE_RULES} "
            "Avoid US-only assumptions (taxes, currency) if the client mentions international users.\n\n"
            "**Required output shape** (use these section headings):\n"
            "- ## FAQ — for each entry: ### Topic or **Q:** question / **A:** answer (2–4 sentences). Use the exact count and topics the client asked for.\n"
            "- ## Tooltips — list or table: screen/location | tooltip text (under 15 words each unless client specifies otherwise).\n"
            "- ## Getting started — numbered steps (e.g. 1. … 2. …), 2–3 sentences per step if the client asks for a guide.\n"
            "Omit a section only if the client did not ask for it. Output only the Markdown document, no preamble."
        )
        try:
            out, usage = await _chat(self, system, user)
            return TaskResult(
                task_id=task.task_id,
                success=True,
                output=(out or "[No help content produced]")[:65536],
                metadata=_metadata_with_usage(
                    {"worker": "HelpContentWorker", "deliverable_type": "markdown"},
                    usage,
                    self.llm,
                ),
            )
        except Exception as e:
            logger.exception("HelpContentWorker failed: %s", e)
            return TaskResult(
                task_id=task.task_id,
                success=False,
                output=f"[HelpContentWorker] Error: {e}",
                metadata={"worker": "HelpContentWorker", "error": str(e)},
            )

    async def get_bid(self, rfp: "RequestForProposal") -> "Bid | None":
        try:
            from sovereign_os.governance.auction import Bid
            return Bid(
                agent_id=self.agent_id,
                estimated_cost_cents=max(1, (rfp.estimated_token_budget * 18) // 1000),
                estimated_time_seconds=15.0,
                confidence_score=0.8,
                model_id=getattr(self.llm, "model_name", "help_docs") if self.llm else "help_docs",
            )
        except Exception:
            return None


class MeetingMinutesWorker(BaseWorker):
    """Turn a transcript into minutes. Output shape: ## Summary, ## Decisions, ## Action items, ## Risks, ## Open questions."""

    async def execute(self, task: TaskInput) -> TaskResult:
        desc = (task.description or "").strip() or task.task_id
        if not self.llm:
            return TaskResult(
                task_id=task.task_id,
                success=True,
                output=f"[MeetingMinutesWorker] No LLM; echo: {desc[:200]}",
                metadata={"worker": "MeetingMinutesWorker", "deliverable_type": "markdown"},
            )
        language = _ctx(task, "language", "English")
        system = (self.system_prompt or "You create crisp, scannable meeting minutes. Preserve who said what and deadlines.").strip()
        user = (
            f"Create meeting minutes in {language} from the transcript below.\n\nTranscript:\n{desc}\n\n"
            "**Output shape** (use these section headings): ## Summary (3–6 bullets), ## Decisions, ## Action items (owner | due date if present), ## Risks / blockers, ## Open questions. "
            "Output only the Markdown document, no preamble."
        )
        try:
            out, usage = await _chat(self, system, user)
            return TaskResult(
                task_id=task.task_id,
                success=True,
                output=(out or "[No minutes produced]")[:65536],
                metadata=_metadata_with_usage(
                    {"worker": "MeetingMinutesWorker", "deliverable_type": "markdown"},
                    usage,
                    self.llm,
                ),
            )
        except Exception as e:
            logger.exception("MeetingMinutesWorker failed: %s", e)
            return TaskResult(
                task_id=task.task_id,
                success=False,
                output=f"[MeetingMinutesWorker] Error: {e}",
                metadata={"worker": "MeetingMinutesWorker", "error": str(e)},
            )


class TranslateWorker(BaseWorker):
    """Translate text while preserving formatting."""

    async def execute(self, task: TaskInput) -> TaskResult:
        desc = (task.description or "").strip() or task.task_id
        if not self.llm:
            return TaskResult(
                task_id=task.task_id,
                success=True,
                output=f"[TranslateWorker] No LLM; echo: {desc[:200]}",
                metadata={"worker": "TranslateWorker", "deliverable_type": "text"},
            )
        target_language = _ctx(task, "target_language", "English")
        style = _ctx(task, "style", "natural, professional")
        system = (self.system_prompt or "You translate accurately and preserve formatting.").strip()
        user = (
            f"Translate the following into {target_language}.\n"
            f"Style: {style}\n\n"
            "Rules: preserve bullet lists, numbering, code blocks, and inline formatting. "
            "Do not add new facts.\n\n"
            f"Text:\n{desc}"
        )
        try:
            out, usage = await _chat(self, system, user)
            return TaskResult(
                task_id=task.task_id,
                success=True,
                output=(out or "[No translation produced]")[:65536],
                metadata=_metadata_with_usage(
                    {"worker": "TranslateWorker", "deliverable_type": "text", "target_language": target_language},
                    usage,
                    self.llm,
                ),
            )
        except Exception as e:
            logger.exception("TranslateWorker failed: %s", e)
            return TaskResult(
                task_id=task.task_id,
                success=False,
                output=f"[TranslateWorker] Error: {e}",
                metadata={"worker": "TranslateWorker", "error": str(e)},
            )


class RewritePolishWorker(BaseWorker):
    """Rewrite/polish text with constraints (no new facts, keep meaning)."""

    async def execute(self, task: TaskInput) -> TaskResult:
        desc = (task.description or "").strip() or task.task_id
        if not self.llm:
            return TaskResult(
                task_id=task.task_id,
                success=True,
                output=f"[RewritePolishWorker] No LLM; echo: {desc[:200]}",
                metadata={"worker": "RewritePolishWorker", "deliverable_type": "markdown"},
            )
        goal = _ctx(task, "goal", "polish for clarity and concision")
        tone = _ctx(task, "tone", "professional")
        language = _ctx(task, "language", "English")
        system = (self.system_prompt or "You rewrite text while preserving meaning.").strip()
        user = (
            f"Rewrite the text in {language}.\n"
            f"Goal: {goal}\nTone: {tone}\n\n"
            "Rules: do not introduce new facts; keep names/numbers unchanged; preserve formatting.\n\n"
            f"Text:\n{desc}\n\n"
            "Output in Markdown:\n- Rewritten version\n- 3–6 bullet change notes (what improved)\n"
        )
        try:
            out, usage = await _chat(self, system, user)
            return TaskResult(
                task_id=task.task_id,
                success=True,
                output=(out or "[No rewrite produced]")[:65536],
                metadata=_metadata_with_usage(
                    {"worker": "RewritePolishWorker", "deliverable_type": "markdown"},
                    usage,
                    self.llm,
                ),
            )
        except Exception as e:
            logger.exception("RewritePolishWorker failed: %s", e)
            return TaskResult(
                task_id=task.task_id,
                success=False,
                output=f"[RewritePolishWorker] Error: {e}",
                metadata={"worker": "RewritePolishWorker", "error": str(e)},
            )


class AssistantChatWorker(BaseWorker):
    """Generic Q&A / conversational assistant when goal does not match a specific skill (e.g. write, translate, minutes)."""

    async def execute(self, task: TaskInput) -> TaskResult:
        desc = (task.description or "").strip() or task.task_id
        if not self.llm:
            return TaskResult(
                task_id=task.task_id,
                success=True,
                output=f"[AssistantChatWorker] No LLM; echo: {desc[:200]}",
                metadata={"worker": "AssistantChatWorker", "deliverable_type": "text"},
            )
        system = (
            self.system_prompt
            or "You are a helpful assistant. Answer concisely and accurately. If the request is ambiguous, ask one short clarifying question or state assumptions."
        ).strip()
        user = f"Request:\n{desc}\n\nRespond clearly. If the request is a question, answer it. If it is a task, complete it briefly or outline next steps."
        try:
            out, usage = await _chat(self, system, user)
            return TaskResult(
                task_id=task.task_id,
                success=True,
                output=(out or "[No response]")[:65536],
                metadata=_metadata_with_usage(
                    {"worker": "AssistantChatWorker", "deliverable_type": "text"},
                    usage,
                    self.llm,
                ),
            )
        except Exception as e:
            logger.exception("AssistantChatWorker failed: %s", e)
            return TaskResult(
                task_id=task.task_id,
                success=False,
                output=f"[AssistantChatWorker] Error: {e}",
                metadata={"worker": "AssistantChatWorker", "error": str(e)},
            )

    async def get_bid(self, rfp: "RequestForProposal") -> "Bid | None":
        try:
            from sovereign_os.governance.auction import Bid

            return Bid(
                agent_id=self.agent_id,
                estimated_cost_cents=max(1, (rfp.estimated_token_budget * 15) // 1000),
                estimated_time_seconds=10.0,
                confidence_score=0.7,
                model_id=getattr(self.llm, "model_name", "chat") if self.llm else "chat",
            )
        except Exception:
            return None
