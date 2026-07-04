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


def _make_test_verifier(root: str, cmd: str | None):
    """
    Build a verifier() -> (passed, feedback) that runs the repo's test suite via the
    run_tests connector. When execution is disabled (dry-run) it returns (True, why)
    so it never blocks — verification only gates when tests can actually run.
    """
    from sovereign_os.connectors import dispatch

    def verify() -> tuple[bool, str]:
        r = dispatch("run_tests", root=root, action="run_tests", cmd=cmd)
        if r.get("dry_run"):
            return True, "tests skipped (execution disabled — set SOVEREIGN_CODE_EXEC_ENABLED to enforce)"
        passed = bool(r.get("passed"))
        return passed, f"rc={r.get('rc')} passed={passed}\n{(r.get('output') or '')[:1500]}"

    return verify


async def _chat(worker: BaseWorker, system: str, user: str) -> tuple[str, dict[str, int] | None]:
    assert worker.llm is not None
    content = await worker.llm.chat(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
    )
    usage = getattr(worker.llm, "_last_usage", None)
    return (content or "").strip(), usage


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
            from sovereign_os.agents.worker_tools import code_workspace_tools, use_tools_enabled

            tool_calls = 0
            tests_verified: bool | None = None
            workspace_root = _ctx(task, "workspace_root", "")
            if use_tools_enabled(task.context) and workspace_root:
                handlers, descs = code_workspace_tools(workspace_root)
                # Verification-driven delivery: the loop won't accept "done" until the
                # test suite actually passes (when execution is enabled). This is what
                # makes coding delivery top-tier — and keeps broken code from ever being
                # submitted to a paid bounty.
                verifier = _make_test_verifier(workspace_root, _ctx(task, "test_cmd", "") or None)
                out, usage, log, verified = await self.run_with_verified_tools(
                    system, user, handlers, verifier=verifier, descriptions=descs,
                    max_steps=8, max_verify_rounds=3,
                )
                # Count real tool invocations, not internal verification checks.
                tool_calls = sum(1 for e in log if e.get("tool") != "__verify__")
                tests_verified = verified
            else:
                out, usage = await _chat(self, system, user)
            meta = {"worker": "CodeAssistantWorker", "deliverable_type": "markdown",
                    "model_id": getattr(self.llm, "model_name", "default"), "tool_calls": tool_calls}
            if tests_verified is not None:
                meta["tests_verified"] = tests_verified
            if usage:
                meta["input_tokens"], meta["output_tokens"] = usage.get("input_tokens", 0), usage.get("output_tokens", 0)
            # success reflects verification: only False when tests actually ran and did
            # not pass within the repair budget (skip/dry-run leaves tests_verified True).
            return TaskResult(
                task_id=task.task_id,
                success=tests_verified is not False,
                output=(out or "[No analysis]")[:65536],
                metadata=meta,
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
            out, usage = await _chat(self, system, user)
            meta = {"worker": "CodeReviewWorker", "deliverable_type": "markdown", "model_id": getattr(self.llm, "model_name", "default")}
            if usage:
                meta["input_tokens"], meta["output_tokens"] = usage.get("input_tokens", 0), usage.get("output_tokens", 0)
            return TaskResult(task_id=task.task_id, success=True, output=(out or "[No review]")[:65536], metadata=meta)
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
