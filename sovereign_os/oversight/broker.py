"""
OversightBroker: Sovereign-OS as the governance layer over outbound work
(agent posts a task, an external human/agent does it).

Two gates wrap the platform's escrow lifecycle:
  - Budget gate  (CFO / Treasury): no task is posted/funded unless the spend is
    approved against balance, daily cap, per-task ceiling, and runway.
  - Quality gate (Auditor / ReviewEngine): escrow is released ONLY if the
    delivered work passes audit against the task's completion criteria;
    otherwise it is disputed/refunded.

Platform-agnostic: any client exposing post_bounty / fund_escrow / complete /
release / dispute / cancel (e.g. RentAHumanClient) can be governed. Spend is
recorded in the UnifiedLedger on release, so the ledger stays the single source
of truth.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from sovereign_os.agents.base import TaskResult
from sovereign_os.auditor.review_engine import ReviewEngine, value_aware_min_score
from sovereign_os.governance.exceptions import FiscalInsolvencyError, HumanApprovalRequiredError
from sovereign_os.governance.strategist import PlannedTask
from sovereign_os.governance.treasury import Treasury

logger = logging.getLogger(__name__)


class EscrowClient(Protocol):
    """Minimal escrow lifecycle a broker can govern."""

    def post_bounty(self, *, title: str, description: str, price_cents: int, completion_criteria: str = "") -> dict: ...
    def fund_escrow(self, bounty_id: str, amount_cents: int) -> dict: ...
    def complete(self, escrow_id: str) -> dict: ...
    def release(self, escrow_id: str) -> dict: ...
    def dispute(self, escrow_id: str) -> dict: ...
    def cancel(self, escrow_id: str) -> dict: ...


class OversightBroker:
    def __init__(
        self,
        treasury: Treasury,
        review_engine: ReviewEngine,
        client: EscrowClient,
        *,
        ledger: Any = None,
        registry: Any = None,
    ) -> None:
        self._treasury = treasury
        self._review = review_engine
        self._client = client
        self._ledger = ledger
        self._registry = registry

    def escrow_status(self, escrow_id: str) -> str:
        """Current platform-side status of an escrow (e.g. funded|delivered|released)."""
        try:
            return str(self._client.get_escrow(escrow_id).get("status") or "")  # type: ignore[attr-defined]
        except Exception as e:  # pragma: no cover - network/path issues
            logger.warning("OVERSIGHT: escrow_status(%s) failed: %s", escrow_id, e)
            return ""

    # ------------------------------------------------------ budget gate
    def post_governed_task(
        self,
        *,
        title: str,
        description: str,
        price_cents: int,
        required_skill: str = "general",
        completion_criteria: str = "",
    ) -> dict[str, Any]:
        """
        CFO budget gate, then post + fund the task. Returns
        {posted, reason, bounty_id, escrow_id, price_cents}.
        Nothing is funded unless Treasury approves the spend.
        """
        try:
            self._treasury.approve_task(price_cents, task_id=title[:40], purpose="hire external worker")
        except (FiscalInsolvencyError, HumanApprovalRequiredError) as e:
            logger.warning("OVERSIGHT: budget gate REJECTED post '%s': %s", title[:40], e)
            self._record(escrow_id=f"rejected:{title[:40]}", title=title, price_cents=price_cents,
                         status="rejected", required_skill=required_skill,
                         completion_criteria=completion_criteria, reason=str(e))
            return {"posted": False, "reason": str(e), "price_cents": price_cents}

        bounty = self._client.post_bounty(
            title=title, description=description, price_cents=price_cents,
            completion_criteria=completion_criteria,
        )
        bounty_id = str(bounty.get("id") or "")
        escrow = self._client.fund_escrow(bounty_id, price_cents)
        escrow_id = str(escrow.get("id") or bounty_id)
        # Reserve the committed funds NOW (escrow is funded), so concurrent posts
        # see the reduced balance and can't over-commit. Release keeps it; a
        # dispute refunds it.
        if self._ledger is not None and hasattr(self._ledger, "record_usd"):
            self._ledger.record_usd(-abs(price_cents), purpose="escrow_reserve", ref=f"escrow-{escrow_id}")
        logger.info("OVERSIGHT: posted+funded '%s' (escrow=%s, $%.2f).", title[:40], escrow_id, price_cents / 100.0)
        self._record(escrow_id=escrow_id, title=title, price_cents=price_cents, status="funded",
                     bounty_id=bounty_id, required_skill=required_skill,
                     completion_criteria=completion_criteria)
        return {
            "posted": True,
            "reason": "",
            "bounty_id": bounty_id,
            "escrow_id": escrow_id,
            "price_cents": price_cents,
            "required_skill": required_skill,
            "completion_criteria": completion_criteria,
        }

    # ----------------------------------------------------- quality gate
    async def review_and_settle(
        self,
        *,
        escrow_id: str,
        deliverable: str,
        task_description: str,
        price_cents: int,
        required_skill: str = "general",
        completion_criteria: str = "",
        value_aware: bool = True,
    ) -> dict[str, Any]:
        """
        Auditor quality gate on the delivered work. On pass: complete + release
        (and record the spend). On fail: dispute (funds frozen, not paid).
        Higher-value tasks are held to a stricter bar when value_aware=True.
        """
        min_score = value_aware_min_score(price_cents) if value_aware else None
        task = PlannedTask(
            task_id=escrow_id,
            description=(completion_criteria or task_description),
            dependencies=[],
            required_skill=required_skill,
            estimated_token_budget=0,
            priority="high",
        )
        result = TaskResult(task_id=escrow_id, success=True, output=deliverable)
        report = await self._review.audit_task(task, result, min_score=min_score)

        if report.passed:
            self._client.complete(escrow_id)
            self._client.release(escrow_id)
            # Funds were already reserved at funding time — release keeps them, no
            # second debit.
            logger.info("OVERSIGHT: quality PASS (%.2f) — released $%.2f for %s.",
                        report.score, price_cents / 100.0, escrow_id)
            if self._registry is not None:
                self._registry.update(escrow_id, status="released", score=report.score)
            return {"action": "released", "paid": True, "score": report.score, "report": report}

        self._client.dispute(escrow_id)
        # Refund the reservation — disputed work is not paid for.
        if self._ledger is not None and hasattr(self._ledger, "record_usd"):
            self._ledger.record_usd(abs(price_cents), purpose="escrow_refund", ref=f"escrow-{escrow_id}")
        logger.warning("OVERSIGHT: quality FAIL (%.2f) — disputed %s (refunded, not paid). Reason: %s",
                       report.score, escrow_id, report.reason)
        if self._registry is not None:
            self._registry.update(escrow_id, status="disputed", score=report.score, reason=report.reason)
        return {"action": "disputed", "paid": False, "score": report.score, "report": report}

    def _record(self, **fields) -> None:
        if self._registry is None:
            return
        from sovereign_os.oversight.registry import EscrowRecord

        try:
            self._registry.add(EscrowRecord(**fields))
        except Exception as e:  # pragma: no cover - best-effort
            logger.warning("OVERSIGHT: registry record failed: %s", e)
