"""
Autonomous Task Bidding & Auction Logic.

When the CEO plans a task, the BiddingEngine broadcasts a RequestForProposal (RFP)
to the WorkerRegistry; registered agents submit Bids. The CFO (Treasury) selects
the winner by Utility Score. Agents that win but fail audits are penalized (TrustScore
drop), making future bids less competitive.
"""

import logging
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from sovereign_os.agents.registry import WorkerRegistry
    from sovereign_os.models.charter import Charter


# ---------------------------------------------------------------------------
# RFP & Bid models
# ---------------------------------------------------------------------------


class RequestForProposal(BaseModel):
    """Broadcast to workers when the CEO plans a task; agents respond with Bids."""

    task_id: str = ""
    description: str = ""
    required_skill: str = ""
    estimated_token_budget: int = Field(ge=0, default=0)
    priority: str = "low"  # high | low
    deadline_seconds: float | None = None  # optional hard deadline


class Bid(BaseModel):
    """Agent's response to an RFP: cost, time, confidence. CFO uses these to select winner."""

    agent_id: str = ""
    estimated_cost_cents: int = Field(ge=0, default=0)
    estimated_time_seconds: float = Field(ge=0, default=0)
    confidence_score: float = Field(ge=0, le=1, default=0.5)  # 0–1
    model_id: str = ""  # e.g. gpt-4o-mini, o1-preview
    suggested_max_tokens: int | None = None  # optional cap to fit runway


# ---------------------------------------------------------------------------
# BiddingEngine
# ---------------------------------------------------------------------------


class BiddingEngine:
    """
    Broadcasts RFP to the WorkerRegistry; collects Bids from all agents
    that can fulfill the required_skill.
    """

    def __init__(
        self,
        registry: "WorkerRegistry",
        charter: "Charter",
    ) -> None:
        self._registry = registry
        self._charter = charter

    async def broadcast_rfp(self, rfp: RequestForProposal) -> list[Bid]:
        """
        Send RFP to all registered bidders for rfp.required_skill;
        return list of Bids (one per bidder).
        """
        bidders = self._registry.get_bidders(rfp.required_skill)
        if not bidders:
            logger.warning("AUCTION: No bidders for skill %s; returning empty bid list.", rfp.required_skill)
            return []

        bids: list[Bid] = []
        for agent_id, worker_class in bidders:
            try:
                worker = self._registry.get_worker(rfp.required_skill, agent_id, task_description=rfp.description)
                if hasattr(worker, "get_bid") and callable(getattr(worker, "get_bid")):
                    bid = await worker.get_bid(rfp)  # type: ignore[attr-defined]
                else:
                    bid = _default_bid_for_rfp(agent_id, rfp, worker_class)
                if bid:
                    bids.append(bid)
            except Exception as e:
                logger.warning("AUCTION: Bid from %s failed: %s", agent_id, e)
        logger.info("AUCTION: RFP %s received %d bid(s).", rfp.task_id, len(bids))
        return bids


def _default_bid_for_rfp(agent_id: str, rfp: RequestForProposal, worker_class: type) -> Bid:
    """Heuristic bid when worker does not implement get_bid (e.g. StubWorker)."""
    # Rough cost from token budget (~10 cents per 1k tokens)
    cents = max(1, (rfp.estimated_token_budget * 10) // 1000)
    # Stub / generic: medium confidence
    confidence = 0.6
    model_id = getattr(worker_class, "_model_id", "") or "stub"
    return Bid(
        agent_id=agent_id,
        estimated_cost_cents=cents,
        estimated_time_seconds=30.0,
        confidence_score=confidence,
        model_id=model_id,
    )
