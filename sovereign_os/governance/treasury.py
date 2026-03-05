"""
Treasury: The CFO Mind — fiscal gatekeeping and model selection.

Integrates with UnifiedLedger to approve/deny task budgets and to choose
cost-effective models by task complexity. Runs winner determination for
RFP auctions: Utility = (Confidence_Score / Estimated_Cost) * Priority_Multiplier,
with TrustScore discount. Can negotiate (e.g. smaller context) to fit runway.
"""

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from sovereign_os.governance.exceptions import FiscalInsolvencyError
from sovereign_os.ledger.unified_ledger import UnifiedLedger
from sovereign_os.models.charter import Charter

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from sovereign_os.agents.auth import SovereignAuth
    from sovereign_os.governance.auction import Bid


# Default minimum reserve (cents) to keep in the ledger before approving new spend
DEFAULT_MIN_RESERVE_CENTS = 0


def _start_of_today_utc() -> datetime:
    now = datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


class Treasury:
    """
    CFO logic: budget approval and token-hedging (model selection).

    - approve_task: enforces balance vs min_reserve and daily burn cap.
    - get_optimal_model: returns model ID by task complexity (high -> o1, low -> gpt-4o-mini).
    """

    def __init__(
        self,
        charter: Charter,
        ledger: UnifiedLedger,
        *,
        min_reserve_cents: int = DEFAULT_MIN_RESERVE_CENTS,
    ) -> None:
        self._charter = charter
        self._ledger = ledger
        self._min_reserve_cents = min_reserve_cents

    @property
    def _daily_burn_max_cents(self) -> int:
        return int(self._charter.fiscal_boundaries.daily_burn_max_usd * 100)

    def approve_task(self, estimated_cost_cents: int, *, task_id: str = "", purpose: str = "") -> None:
        """
        Check fiscal constraints for a task. Raises FiscalInsolvencyError if denied.

        - Ensures current_balance - estimated_cost >= min_reserve.
        - Ensures daily_spend + estimated_cost <= charter.fiscal_boundaries.daily_burn_max_usd (in cents).
        """
        balance_cents = self._ledger.total_usd_cents()
        if balance_cents - estimated_cost_cents < self._min_reserve_cents:
            msg = (
                f"CFO denied budget: balance {balance_cents} cents - estimated cost {estimated_cost_cents} cents "
                f"would fall below min reserve {self._min_reserve_cents} cents."
            )
            logger.warning("GOVERNANCE CFO: %s", msg)
            raise FiscalInsolvencyError(
                msg,
                balance_cents=balance_cents,
                requested_cents=estimated_cost_cents,
            )

        daily_spend_cents = self._ledger.usd_debits_since(_start_of_today_utc())
        if self._daily_burn_max_cents > 0 and daily_spend_cents + estimated_cost_cents > self._daily_burn_max_cents:
            msg = (
                f"CFO denied budget: daily spend {daily_spend_cents} + estimated {estimated_cost_cents} "
                f"exceeds daily cap {self._daily_burn_max_cents} cents."
            )
            logger.warning("GOVERNANCE CFO: %s", msg)
            raise FiscalInsolvencyError(
                msg,
                balance_cents=balance_cents,
                requested_cents=estimated_cost_cents,
            )

        usd = estimated_cost_cents / 100.0
        logger.info(
            "GOVERNANCE CFO: Approved $%.2f budget for task (task_id=%s, purpose=%s).",
            usd,
            task_id or "unknown",
            purpose or "unspecified",
        )

    def get_optimal_model(self, task_complexity: str) -> str:
        """
        Return the most cost-effective model ID for the given complexity.

        - High priority / complex -> reasoning model (e.g. o1-preview).
        - Low priority / simple -> low-latency cheap model (e.g. gpt-4o-mini).
        """
        c = task_complexity.strip().lower()
        if c in ("high", "complex", "critical", "reasoning"):
            return "o1-preview"
        return "gpt-4o-mini"

    # -------------------------------------------------------------------------
    # Auction: winner determination and dynamic budgeting
    # -------------------------------------------------------------------------

    def _priority_multiplier(self, priority: str) -> float:
        """Priority multiplier for utility: high -> 1.5, low -> 1.0."""
        return 1.5 if (priority or "").strip().lower() in ("high", "complex", "critical") else 1.0

    def select_winner(
        self,
        bids: list["Bid"],
        task_priority: str = "low",
        *,
        auth: "SovereignAuth | None" = None,
    ) -> "Bid | None":
        """
        Select winner by Utility Score = (Confidence_Score / Estimated_Cost) * Priority_Multiplier.
        Agents with lower TrustScore get a discount (utility *= TrustScore/100), so audit failures
        make future bids less competitive.
        """
        if not bids:
            return None
        mult = self._priority_multiplier(task_priority)
        best: tuple[float, Bid] = (0.0, bids[0])
        for bid in bids:
            cost = max(1, bid.estimated_cost_cents)
            utility = (bid.confidence_score / cost) * mult
            if auth is not None:
                trust = auth.get_trust_score(bid.agent_id)
                utility *= trust / 100.0
            if utility > best[0]:
                best = (utility, bid)
        logger.info(
            "GOVERNANCE CFO: Winner %s (utility=%.4f) for task priority=%s.",
            best[1].agent_id,
            best[0],
            task_priority,
        )
        return best[1]

    def negotiate(
        self,
        bid: "Bid",
        remaining_runway_cents: int,
        *,
        min_cents: int = 1,
    ) -> "Bid":
        """
        Dynamic budgeting: if bid cost exceeds remaining runway, suggest a smaller
        context (e.g. suggested_max_tokens) so the agent can fit the budget.
        Returns the same bid with suggested_max_tokens set if negotiation applied.
        """
        if bid.estimated_cost_cents <= remaining_runway_cents or remaining_runway_cents < min_cents:
            return bid
        # Scale down: suggest tokens proportional to runway (rough: 1k tokens ~ 10 cents)
        suggested_tokens = max(256, (remaining_runway_cents * 1000) // 10)
        negotiated = bid.model_copy(update={"suggested_max_tokens": suggested_tokens})
        logger.info(
            "GOVERNANCE CFO: Negotiated bid from %s: suggested_max_tokens=%d to fit runway %d cents.",
            bid.agent_id,
            suggested_tokens,
            remaining_runway_cents,
        )
        return negotiated
