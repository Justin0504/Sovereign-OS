"""
Top-tier specialist workers for high-frequency delivery categories that the
built-in set didn't cover well: design and data. LLM-only (agents can't render
pixels or run a warehouse), so they produce rigorous, implementation-ready
deliverables a downstream human or tool can execute directly.
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
        return str((task.context or {}).get(key, default)).strip()
    except Exception:
        return default


def _brief(task: TaskInput) -> str:
    return (_ctx(task, "original_goal", "") or (task.description or "").strip() or task.task_id)


async def _chat(worker: BaseWorker, system: str, user: str) -> tuple[str, dict[str, int] | None]:
    assert worker.llm is not None
    content = await worker.llm.chat(
        [{"role": "system", "content": system}, {"role": "user", "content": user}]
    )
    return (content or "").strip(), getattr(worker.llm, "_last_usage", None)


def _result(worker: BaseWorker, task: TaskInput, out: str, usage, name: str) -> TaskResult:
    meta = {"worker": name, "deliverable_type": "markdown", "model_id": getattr(worker.llm, "model_name", "default")}
    if usage:
        meta["input_tokens"], meta["output_tokens"] = usage.get("input_tokens", 0), usage.get("output_tokens", 0)
    return TaskResult(task_id=task.task_id, success=True, output=(out or "[empty]")[:65536], metadata=meta)


_DESIGN_SYSTEM = (
    "You are a senior product designer producing an implementation-ready design deliverable. "
    "You cannot render pixels, so you deliver a rigorous design spec a developer or Figma agent can build directly. "
    "Be concrete: name components, states, tokens, spacing, and copy. Never hand-wave."
)

_DATA_SYSTEM = (
    "You are a senior data analyst. You deliver correct, reproducible analysis. "
    "State assumptions, show the method, and never invent numbers — if a value isn't given, mark it as an input to compute. "
    "Prefer tables and explicit, runnable transformation steps (SQL/pandas) over prose."
)


class DesignBriefWorker(BaseWorker):
    """Design category: deliver a build-ready design spec (components, tokens, layout, copy)."""

    async def execute(self, task: TaskInput) -> TaskResult:
        brief = _brief(task)
        if not self.llm:
            return TaskResult(task_id=task.task_id, success=True,
                              output=f"[DesignBriefWorker] No LLM; echo: {brief[:200]}",
                              metadata={"worker": "DesignBriefWorker", "deliverable_type": "markdown"})
        system = (self.system_prompt or _DESIGN_SYSTEM).strip()
        user = (
            f"Design request (follow exactly):\n{brief}\n\n"
            "Deliver Markdown with these exact sections:\n"
            "- ## Goal & Users — who it's for and the job-to-be-done\n"
            "- ## Information Architecture — screens/sections and how they connect\n"
            "- ## Component Spec — each component with states (default/hover/active/disabled/error) and behavior\n"
            "- ## Design Tokens — color, type scale, spacing, radius (concrete values)\n"
            "- ## Layout — responsive structure (mobile + desktop), grid, key dimensions\n"
            "- ## Copy — exact microcopy for the primary screen\n"
            "- ## Handoff Notes — what a developer / Figma agent needs to build it\n"
            "Be specific enough to build without further questions."
        )
        try:
            out, usage = await _chat(self, system, user)
            return _result(self, task, out, usage, "DesignBriefWorker")
        except Exception as e:
            logger.exception("DesignBriefWorker failed: %s", e)
            return TaskResult(task_id=task.task_id, success=False,
                              output=f"[DesignBriefWorker] Error: {e}", metadata={"worker": "DesignBriefWorker", "error": str(e)})

    async def get_bid(self, rfp: "RequestForProposal") -> "Bid | None":
        try:
            from sovereign_os.governance.auction import Bid
            return Bid(agent_id=self.agent_id, estimated_cost_cents=max(2, (rfp.estimated_token_budget * 22) // 1000),
                      estimated_time_seconds=18.0, confidence_score=0.74,
                      model_id=getattr(self.llm, "model_name", "design") if self.llm else "design")
        except Exception:
            return None


class DataAnalysisWorker(BaseWorker):
    """Data category: deliver reproducible analysis (method, transformations, tables, findings)."""

    async def execute(self, task: TaskInput) -> TaskResult:
        brief = _brief(task)
        if not self.llm:
            return TaskResult(task_id=task.task_id, success=True,
                              output=f"[DataAnalysisWorker] No LLM; echo: {brief[:200]}",
                              metadata={"worker": "DataAnalysisWorker", "deliverable_type": "markdown"})
        data = _ctx(task, "data", "")
        system = (self.system_prompt or _DATA_SYSTEM).strip()
        user = (
            f"Analysis request (follow exactly):\n{brief}\n\n"
            + (f"Provided data:\n```\n{data[:6000]}\n```\n\n" if data else "No dataset provided — define the inputs required.\n\n")
            + "Deliver Markdown with these exact sections:\n"
            "- ## Question — what is being answered\n"
            "- ## Assumptions & Inputs — list every assumption and required input (never invent values)\n"
            "- ## Method — the analysis approach, step by step\n"
            "- ## Transformations — runnable SQL or pandas for each step\n"
            "- ## Results — tables; mark any value that needs real data as `<input>`\n"
            "- ## Findings & Caveats — what it means and where it could be wrong\n"
        )
        try:
            out, usage = await _chat(self, system, user)
            return _result(self, task, out, usage, "DataAnalysisWorker")
        except Exception as e:
            logger.exception("DataAnalysisWorker failed: %s", e)
            return TaskResult(task_id=task.task_id, success=False,
                              output=f"[DataAnalysisWorker] Error: {e}", metadata={"worker": "DataAnalysisWorker", "error": str(e)})

    async def get_bid(self, rfp: "RequestForProposal") -> "Bid | None":
        try:
            from sovereign_os.governance.auction import Bid
            return Bid(agent_id=self.agent_id, estimated_cost_cents=max(2, (rfp.estimated_token_budget * 24) // 1000),
                      estimated_time_seconds=18.0, confidence_score=0.73,
                      model_id=getattr(self.llm, "model_name", "data") if self.llm else "data")
        except Exception:
            return None


_TESTGEN_SYSTEM = (
    "You are a senior test engineer. From a code snippet or spec, you produce a rigorous, "
    "runnable unit-test suite. Cover the happy path, edge cases, error paths, and boundaries. "
    "Use the language's idiomatic framework (pytest for Python, jest for JS). Tests must be "
    "deterministic and self-contained; never test against network or randomness. Output only code."
)


class TestGenWorker(BaseWorker):
    """Coding (second tier): generate a rigorous unit-test suite from a code/spec brief."""

    async def execute(self, task: TaskInput) -> TaskResult:
        brief = _brief(task)
        if not self.llm:
            return TaskResult(task_id=task.task_id, success=True,
                              output=f"[TestGenWorker] No LLM; echo: {brief[:200]}",
                              metadata={"worker": "TestGenWorker", "deliverable_type": "code"})
        code = _ctx(task, "code", "")
        language = _ctx(task, "language", "")
        system = (self.system_prompt or _TESTGEN_SYSTEM).strip()
        user = (
            f"Generate tests for the following request (follow exactly):\n{brief}\n\n"
            + (f"Code under test ({language or 'unknown'}):\n```\n{code[:6000]}\n```\n\n" if code else "No code provided — infer the interface from the spec and note assumptions.\n\n")
            + "Deliver: a short '## Coverage plan' (bullets of what you test and why), then a fenced "
            "code block with the complete, runnable test file. Include happy-path, edge, error, and boundary cases."
        )
        try:
            out, usage = await _chat(self, system, user)
            r = _result(self, task, out, usage, "TestGenWorker")
            r.metadata["deliverable_type"] = "code"
            return r
        except Exception as e:
            logger.exception("TestGenWorker failed: %s", e)
            return TaskResult(task_id=task.task_id, success=False,
                              output=f"[TestGenWorker] Error: {e}", metadata={"worker": "TestGenWorker", "error": str(e)})

    async def get_bid(self, rfp: "RequestForProposal") -> "Bid | None":
        try:
            from sovereign_os.governance.auction import Bid
            return Bid(agent_id=self.agent_id, estimated_cost_cents=max(2, (rfp.estimated_token_budget * 26) // 1000),
                      estimated_time_seconds=18.0, confidence_score=0.72,
                      model_id=getattr(self.llm, "model_name", "testgen") if self.llm else "testgen")
        except Exception:
            return None
