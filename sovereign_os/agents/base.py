"""
BaseWorker: Abstract base for all agents. Async execution and Pydantic message passing.
Optional get_bid(RFP) for auction participation.
"""

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
