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


class ArticleWriterWorker(BaseWorker):
    """Write a structured article draft (outline + draft + suggested title)."""

    async def execute(self, task: TaskInput) -> TaskResult:
        desc = (task.description or "").strip() or task.task_id
        if not self.llm:
            return TaskResult(
                task_id=task.task_id,
                success=True,
                output=f"[ArticleWriterWorker] No LLM; echo: {desc[:200]}",
                metadata={"worker": "ArticleWriterWorker", "deliverable_type": "markdown"},
            )
        topic = _ctx(task, "topic") or desc
        audience = _ctx(task, "audience", "general audience")
        tone = _ctx(task, "tone", "professional, clear")
        length = _ctx(task, "length", "800-1200 words")
        language = _ctx(task, "language", "English")
        system = (self.system_prompt or "You write high-quality deliverables.").strip()
        user = (
            f"Write an article in {language}.\n"
            f"Topic: {topic}\nAudience: {audience}\nTone: {tone}\nLength: {length}\n\n"
            "Output in Markdown with these sections:\n"
            "1) Title (3 options)\n2) Outline\n3) Draft\n4) 5-bullet takeaway summary\n"
            "Rules: do not invent quotes or statistics; if facts are uncertain, mark them as assumptions."
        )
        try:
            out = await _chat(self, system, user)
            return TaskResult(
                task_id=task.task_id,
                success=True,
                output=(out or "[No article produced]")[:65536],
                metadata={
                    "worker": "ArticleWriterWorker",
                    "deliverable_type": "markdown",
                    "topic": topic[:200],
                },
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
    """Solve a problem/question with steps, then final answer."""

    async def execute(self, task: TaskInput) -> TaskResult:
        desc = (task.description or "").strip() or task.task_id
        if not self.llm:
            return TaskResult(
                task_id=task.task_id,
                success=True,
                output=f"[ProblemSolverWorker] No LLM; echo: {desc[:200]}",
                metadata={"worker": "ProblemSolverWorker", "deliverable_type": "markdown"},
            )
        system = (self.system_prompt or "You solve problems accurately and clearly.").strip()
        user = (
            "Solve the following problem.\n\n"
            f"Problem:\n{desc}\n\n"
            "Output in Markdown with:\n"
            "- Understanding\n- Step-by-step solution\n- Final answer\n"
            "If information is missing, ask up to 5 clarifying questions first, then provide a best-effort solution with explicit assumptions."
        )
        try:
            out = await _chat(self, system, user)
            return TaskResult(
                task_id=task.task_id,
                success=True,
                output=(out or "[No solution produced]")[:65536],
                metadata={"worker": "ProblemSolverWorker", "deliverable_type": "markdown"},
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
    """Draft an email (subjects + body)."""

    async def execute(self, task: TaskInput) -> TaskResult:
        desc = (task.description or "").strip() or task.task_id
        if not self.llm:
            return TaskResult(
                task_id=task.task_id,
                success=True,
                output=f"[EmailWriterWorker] No LLM; echo: {desc[:200]}",
                metadata={"worker": "EmailWriterWorker", "deliverable_type": "markdown"},
            )
        to = _ctx(task, "to", "customer")
        purpose = _ctx(task, "purpose", "follow up")
        tone = _ctx(task, "tone", "professional, friendly")
        language = _ctx(task, "language", "English")
        system = (self.system_prompt or "You write clear, polite emails.").strip()
        user = (
            f"Write an email in {language}.\n"
            f"To: {to}\nPurpose: {purpose}\nTone: {tone}\n\n"
            f"Context:\n{desc}\n\n"
            "Output in Markdown:\n- Subject (3 options)\n- Email body\n- One short follow-up message (optional)\n"
            "Rules: avoid making promises that are not stated; if policy is unknown, ask clarifying questions."
        )
        try:
            out = await _chat(self, system, user)
            return TaskResult(
                task_id=task.task_id,
                success=True,
                output=(out or "[No email produced]")[:65536],
                metadata={"worker": "EmailWriterWorker", "deliverable_type": "markdown"},
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
    """Draft short social posts."""

    async def execute(self, task: TaskInput) -> TaskResult:
        desc = (task.description or "").strip() or task.task_id
        if not self.llm:
            return TaskResult(
                task_id=task.task_id,
                success=True,
                output=f"[SocialPostWorker] No LLM; echo: {desc[:200]}",
                metadata={"worker": "SocialPostWorker", "deliverable_type": "markdown"},
            )
        platform = _ctx(task, "platform", "X")
        audience = _ctx(task, "audience", "general audience")
        tone = _ctx(task, "tone", "clear, confident")
        language = _ctx(task, "language", "English")
        system = (self.system_prompt or "You write concise posts with strong hooks.").strip()
        user = (
            f"Write social posts in {language} for platform: {platform}.\n"
            f"Audience: {audience}\nTone: {tone}\n\n"
            f"Topic/context:\n{desc}\n\n"
            "Output in Markdown:\n"
            "- 5 variations (A/B/C/D/E)\n"
            "- 5 hashtags (if appropriate)\n"
            "- A short CTA line\n"
            "Rules: respect platform length norms; do not invent numbers."
        )
        try:
            out = await _chat(self, system, user)
            return TaskResult(
                task_id=task.task_id,
                success=True,
                output=(out or "[No post produced]")[:65536],
                metadata={"worker": "SocialPostWorker", "deliverable_type": "markdown", "platform": platform},
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


class MeetingMinutesWorker(BaseWorker):
    """Turn a transcript into decisions + action items."""

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
        system = (self.system_prompt or "You create crisp meeting minutes.").strip()
        user = (
            f"Create meeting minutes in {language} from the transcript below.\n\n"
            f"Transcript:\n{desc}\n\n"
            "Output in Markdown with:\n"
            "- Summary (3–6 bullets)\n"
            "- Decisions\n"
            "- Action items (Owner, Due date if present)\n"
            "- Risks / blockers\n"
            "- Open questions\n"
        )
        try:
            out = await _chat(self, system, user)
            return TaskResult(
                task_id=task.task_id,
                success=True,
                output=(out or "[No minutes produced]")[:65536],
                metadata={"worker": "MeetingMinutesWorker", "deliverable_type": "markdown"},
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
            out = await _chat(self, system, user)
            return TaskResult(
                task_id=task.task_id,
                success=True,
                output=(out or "[No translation produced]")[:65536],
                metadata={"worker": "TranslateWorker", "deliverable_type": "text", "target_language": target_language},
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
            out = await _chat(self, system, user)
            return TaskResult(
                task_id=task.task_id,
                success=True,
                output=(out or "[No rewrite produced]")[:65536],
                metadata={"worker": "RewritePolishWorker", "deliverable_type": "markdown"},
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
            out = await _chat(self, system, user)
            return TaskResult(
                task_id=task.task_id,
                success=True,
                output=(out or "[No response]")[:65536],
                metadata={"worker": "AssistantChatWorker", "deliverable_type": "text"},
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
