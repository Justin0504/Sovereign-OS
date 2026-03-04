"""
Treasury: The CFO Mind — fiscal gatekeeping and model selection.

Integrates with UnifiedLedger to approve/deny task budgets and to choose
cost-effective models by task complexity.
"""

import logging
from datetime import datetime, timezone

from sovereign_os.governance.exceptions import FiscalInsolvencyError
from sovereign_os.ledger.unified_ledger import UnifiedLedger
from sovereign_os.models.charter import Charter

logger = logging.getLogger(__name__)


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
