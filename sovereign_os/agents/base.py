"""
BaseWorker: Abstract base for all agents. Async execution and Pydantic message passing.
Optional get_bid(RFP) for auction participation.
"""

import inspect
from abc import ABC, abstractmethod
from typing import Annotated, TYPE_CHECKING, Any

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from sovereign_os.governance.auction import Bid, RequestForProposal
    from sovereign_os.llm.providers import ChatLLM


# ---------------------------------------------------------------------------
# Message types (Pydantic for type-safe passing between agents)
# ---------------------------------------------------------------------------


class TaskInput(BaseModel):
    """Input passed to a worker for a single task."""

    task_id: Annotated[str, Field(min_length=1)]
    description: str = ""
    required_skill: str = ""
    context: dict[str, str] = Field(default_factory=dict)


class TaskResult(BaseModel):
    """Result returned by a worker for the (upcoming) Auditor to verify."""

    task_id: Annotated[str, Field(min_length=1)]
    success: bool = True
    output: str = ""
    metadata: dict[str, str | int | float | bool] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# BaseWorker
# ---------------------------------------------------------------------------


class BaseWorker(ABC):
    """
    Abstract worker. All agents support async execution and receive
    a system prompt derived from the Charter for mission alignment.
    Override get_bid() to participate in RFP auctions with custom bids.
    """

    def __init__(
        self,
        agent_id: str,
        system_prompt: str = "",
        *,
        llm: "ChatLLM | None" = None,
    ) -> None:
        self.agent_id = agent_id
        self.system_prompt = system_prompt
        # Optional shared LLM client for this worker (may be None for pure-tool workers).
        self.llm: Any | None = llm

    @abstractmethod
    async def execute(self, task: TaskInput) -> TaskResult:
        """Run the task asynchronously and return a result for the Auditor."""
        ...

    async def get_bid(self, rfp: "RequestForProposal") -> "Bid | None":
        """
        Optional: return a Bid for the RFP (used by BiddingEngine).
        Default returns None; subclasses or registry default bid logic will be used.
        """
        return None

    # ----------------------------------------------------------- tool helpers
    async def _with_mcp_tools(
        self, tool_handlers: "dict[str, Any]", desc: "dict[str, str]"
    ) -> tuple["dict[str, Any]", "dict[str, str]"]:
        """
        Merge every registered live MCP server's tools into this worker's tool set, so
        any MCP-provided connector is callable inline in the tool-use loop. Built-in
        tools win on name collision. No servers registered (default) -> unchanged.
        """
        try:
            from sovereign_os.mcp.live import has_servers, mcp_tool_handlers

            if not has_servers():
                return tool_handlers, desc
            mcp_handlers, mcp_desc = await mcp_tool_handlers()
            merged = dict(tool_handlers)
            merged_desc = dict(desc)
            for name, handler in mcp_handlers.items():
                if name not in merged:
                    merged[name] = handler
                    merged_desc.setdefault(name, mcp_desc.get(name, ""))
            return merged, merged_desc
        except Exception:  # noqa: BLE001 - MCP must never break the worker
            return tool_handlers, desc

    @staticmethod
    async def _run_handler(handler: "Any", args: dict) -> str:
        """Invoke a tool handler, awaiting it if it's async (MCP handlers are)."""
        res = handler(args)
        if inspect.isawaitable(res):
            res = await res
        return str(res)

    # ----------------------------------------------------------- LLM helpers
    async def _chat_once(self, system: str, user: str) -> tuple[str, dict | None]:
        """One LLM turn; returns (text, usage). Requires self.llm."""
        content = await self.llm.chat(  # type: ignore[union-attr]
            [{"role": "system", "content": system}, {"role": "user", "content": user}]
        )
        return (content or "").strip(), getattr(self.llm, "_last_usage", None)

    async def deliver(self, system: str, user: str, *, revise: bool = False) -> tuple[str, dict | None]:
        """
        Produce a deliverable. When revise=True, run a draft -> self-critique ->
        improved-final pass (top-tier quality) before returning. Usage covers the
        final turn. Caller is responsible for the no-LLM fallback (self.llm check).
        """
        draft, usage = await self._chat_once(system, user)
        if not (revise and self.llm and draft):
            return draft, usage
        crit_system = (system + "\n\nYou hold yourself to a top-tier standard and improve your own work before delivering.").strip()
        crit_user = (
            f"{user}\n\n--- YOUR DRAFT ---\n{draft}\n\n"
            "Critically review the draft against the request: coverage of every requirement, "
            "correctness, specificity (no hand-waving), and format. Then output ONLY the improved "
            "final deliverable — no preamble, no notes about what you changed."
        )
        improved, usage2 = await self._chat_once(crit_system, crit_user)
        return (improved or draft), (usage2 or usage)

    async def run_with_tools(
        self,
        system: str,
        user: str,
        tool_handlers: "dict[str, Any]",
        *,
        max_steps: int = 4,
        descriptions: "dict[str, str] | None" = None,
    ) -> tuple[str, dict | None, list]:
        """
        Provider-agnostic tool-use loop. The model either calls a tool or returns
        the final deliverable, as JSON. Each tool is a callable(args: dict) -> str
        observation. Loops up to max_steps, feeding observations back, then forces
        a final answer. Returns (final_text, usage, tool_log).

        Lets workers gather REAL information mid-task (web_fetch, read_file, run_tests)
        rather than answering from memory — the path to top-tier delivery.
        """
        tool_handlers, desc = await self._with_mcp_tools(tool_handlers, descriptions or {})
        tools_block = "\n".join(f'- {name}: {desc.get(name, "")}' for name in tool_handlers)
        sys = (
            (system or "").strip()
            + "\n\nYou can call tools to gather real information before answering.\n"
            + "Available tools:\n" + tools_block + "\n\n"
            + 'To call a tool respond with ONLY JSON: {"action":"tool","tool":"<name>","args":{...}}.\n'
            + 'When you have enough information respond with ONLY JSON: {"action":"final","output":"<the full deliverable>"}.'
        )
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": sys},
            {"role": "user", "content": user},
        ]
        last_usage: dict | None = None
        log: list = []

        for _ in range(max(1, max_steps)):
            content = await self.llm.chat(messages)  # type: ignore[union-attr]
            last_usage = getattr(self.llm, "_last_usage", None) or last_usage
            action = _parse_action(content or "")
            if action is None or action.get("action") == "final":
                return ((action or {}).get("output") or (content or "")).strip(), last_usage, log
            name = str(action.get("tool", ""))
            args = action.get("args") if isinstance(action.get("args"), dict) else {}
            handler = tool_handlers.get(name)
            if handler is None:
                obs = f"(no such tool: {name})"
            else:
                try:
                    obs = (await self._run_handler(handler, args))[:4000]
                except Exception as e:  # noqa: BLE001 - surfaced to the model
                    obs = f"(tool error: {e})"
            log.append({"tool": name, "args": args, "obs": obs[:200]})
            messages.append({"role": "assistant", "content": content or ""})
            messages.append({"role": "user", "content": f"Tool '{name}' result:\n{obs}\n\nContinue with a tool call or the final JSON."})

        # Out of steps — force a final deliverable.
        messages.append({"role": "user", "content": 'Now output ONLY {"action":"final","output":"<the deliverable>"}.'})
        content = await self.llm.chat(messages)  # type: ignore[union-attr]
        last_usage = getattr(self.llm, "_last_usage", None) or last_usage
        action = _parse_action(content or "")
        final = (action or {}).get("output") if action and action.get("action") == "final" else (content or "")
        return (final or "").strip(), last_usage, log

    async def run_with_verified_tools(
        self,
        system: str,
        user: str,
        tool_handlers: "dict[str, Any]",
        *,
        verifier: "Any",
        max_steps: int = 8,
        max_verify_rounds: int = 3,
        descriptions: "dict[str, str] | None" = None,
    ) -> tuple[str, dict | None, list, bool]:
        """
        Tool-use loop where a proposed final answer is ACCEPTED ONLY after it is
        verified. This is the difference between a toy coding agent and a top-tier
        one: the harness — not the model's goodwill — enforces "keep working until the
        work provably passes." When the model emits `final`, `verifier()` runs; on
        failure its feedback (e.g. failing test output) is fed back and the model must
        continue. Returns (final_text, usage, tool_log, verified).

        `verifier`: callable() -> (passed: bool, feedback: str). Runs the real check
        (e.g. the test suite). If it can't run (e.g. execution disabled), it should
        return (True, "<why skipped>") so it never blocks — verification only *gates*
        when it can actually execute.
        `max_verify_rounds`: how many times a failing verification may bounce back
        before the loop gives up and returns the best attempt with verified=False.
        """
        tool_handlers, desc = await self._with_mcp_tools(tool_handlers, descriptions or {})
        tools_block = "\n".join(f'- {name}: {desc.get(name, "")}' for name in tool_handlers)
        sys = (
            (system or "").strip()
            + "\n\nYou can call tools to do and CHECK real work before answering.\n"
            + "Available tools:\n" + tools_block + "\n\n"
            + "Your deliverable is not accepted until it passes automated verification "
            + "(e.g. the test suite). Do the work, run the check, and fix failures — repeat until it passes.\n"
            + 'To call a tool respond with ONLY JSON: {"action":"tool","tool":"<name>","args":{...}}.\n'
            + 'When you believe it is done and verified respond with ONLY JSON: {"action":"final","output":"<the full deliverable>"}.'
        )
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": sys},
            {"role": "user", "content": user},
        ]
        last_usage: dict | None = None
        log: list = []
        best_final = ""
        verify_rounds = 0

        def _verify() -> tuple[bool, str]:
            try:
                ok, feedback = verifier()
                return bool(ok), str(feedback or "")
            except Exception as e:  # noqa: BLE001 - surfaced to the model
                return False, f"verifier error: {e}"

        for _ in range(max(1, max_steps)):
            content = await self.llm.chat(messages)  # type: ignore[union-attr]
            last_usage = getattr(self.llm, "_last_usage", None) or last_usage
            action = _parse_action(content or "")

            if action is not None and action.get("action") == "final":
                proposed = (action.get("output") or "").strip()
                best_final = proposed or best_final
                ok, feedback = _verify()
                log.append({"tool": "__verify__", "ok": ok, "obs": feedback[:200]})
                if ok:
                    return (proposed or content or "").strip(), last_usage, log, True
                verify_rounds += 1
                if verify_rounds >= max(1, max_verify_rounds):
                    break
                messages.append({"role": "assistant", "content": content or ""})
                messages.append({"role": "user", "content": (
                    f"Verification FAILED:\n{feedback[:3000]}\n\n"
                    "Do not declare done. Use the tools to fix the cause, then re-verify."
                )})
                continue

            if action is None:
                messages.append({"role": "assistant", "content": content or ""})
                messages.append({"role": "user", "content": 'Respond with ONLY a tool call or final JSON.'})
                continue

            name = str(action.get("tool", ""))
            args = action.get("args") if isinstance(action.get("args"), dict) else {}
            handler = tool_handlers.get(name)
            if handler is None:
                obs = f"(no such tool: {name})"
            else:
                try:
                    obs = (await self._run_handler(handler, args))[:4000]
                except Exception as e:  # noqa: BLE001 - surfaced to the model
                    obs = f"(tool error: {e})"
            log.append({"tool": name, "args": args, "obs": obs[:200]})
            messages.append({"role": "assistant", "content": content or ""})
            messages.append({"role": "user", "content": f"Tool '{name}' result:\n{obs}\n\nContinue with a tool call or the final JSON."})

        # Budget exhausted (or too many failed verifications): return best attempt, unverified.
        return (best_final or "").strip(), last_usage, log, False


class StubWorker(BaseWorker):
    """Default worker when no implementation is registered; returns placeholder result for Auditor."""

    _model_id: str = "stub"

    async def get_bid(self, rfp: "RequestForProposal") -> "Bid":
        from sovereign_os.governance.auction import Bid
        cents = max(1, (rfp.estimated_token_budget * 10) // 1000)
        return Bid(
            agent_id=self.agent_id,
            estimated_cost_cents=cents,
            estimated_time_seconds=30.0,
            confidence_score=0.6,
            model_id=self._model_id,
        )

    async def execute(self, task: TaskInput) -> TaskResult:
        return TaskResult(
            task_id=task.task_id,
            success=True,
            output=f"[Stub] Completed: {task.description[:100] or task.task_id}",
            metadata={"worker": "StubWorker"},
        )


def _parse_action(content: str) -> dict | None:
    """Extract a JSON action object from a model reply (tolerates code fences/prose)."""
    import json
    import re

    s = (content or "").strip()
    s = s.removeprefix("```json").removeprefix("```").strip().removesuffix("```").strip()
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else None
    except Exception:
        pass
    m = re.search(r"\{.*\}", s, re.S)  # first JSON-looking object
    if m:
        try:
            obj = json.loads(m.group(0))
            return obj if isinstance(obj, dict) else None
        except Exception:
            return None
    return None
