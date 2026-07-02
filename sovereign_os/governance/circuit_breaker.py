"""
SpendCircuitBreaker: the CFO's fast-fail runtime guardrail for a single session.

The per-task / per-mission / daily-burn gates in `Treasury` are *pre-flight* — they
decide whether to fund the next task. They do not stop a session that is bleeding
money across many small, individually-approved tasks (the failure mode behind the
real 2025 incidents where a pair of looping agents ran for days and burned tens of
thousands of dollars before anyone noticed).

This adds the missing runtime layer FinOps practice calls for: a per-session cost
ceiling plus fast-fail on repeated quality failures and on a collapsing return on
spend. It answers one question continuously — *is this execution path still worth
funding?* — and trips (halts the loop) the moment it isn't.

Three independent trip conditions (any one trips):
  1. Session ceiling  — cumulative spend >= `session_ceiling_cents`.
  2. Consecutive fails — `max_consecutive_failures` audits failed back-to-back.
  3. ROI floor         — once `roi_grace_spend_cents` has been spent, realized
                          revenue / spend drops below `roi_floor` (path not paying off).

Design notes:
  - Off by default: a breaker with all limits 0/None never trips, so existing
    callers keep their behavior until they opt in.
  - Pure in-memory and monotonic-time-free: state is fed in via record_* calls,
    so it is deterministic and trivially unit-testable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sovereign_os.governance.exceptions import CircuitBreakerTrippedError

logger = logging.getLogger(__name__)


@dataclass
class SpendCircuitBreaker:
    """Runtime fast-fail guard for one autonomous session. See module docstring."""

    session_ceiling_cents: int = 0          # 0 => no session cap
    max_consecutive_failures: int = 0       # 0 => never trip on failure streaks
    roi_floor: float = 0.0                  # 0 => ROI check disabled
    roi_grace_spend_cents: int = 0          # don't judge ROI until this much is spent

    spent_cents: int = field(default=0, init=False)
    revenue_cents: int = field(default=0, init=False)
    consecutive_failures: int = field(default=0, init=False)
    _tripped_reason: str = field(default="", init=False)

    # ------------------------------------------------------------------ record
    def record_spend(self, amount_cents: int) -> None:
        """Add realized spend for this session (called when the ledger is debited)."""
        if amount_cents > 0:
            self.spent_cents += int(amount_cents)

    def record_revenue(self, amount_cents: int) -> None:
        """Add realized revenue for this session (called when a job is collected)."""
        if amount_cents > 0:
            self.revenue_cents += int(amount_cents)

    def record_outcome(self, passed: bool) -> None:
        """Feed an audit outcome so the failure-streak trip can fire."""
        self.consecutive_failures = 0 if passed else self.consecutive_failures + 1

    # ------------------------------------------------------------------- state
    @property
    def is_tripped(self) -> bool:
        return bool(self._trip_reason())

    def _trip_reason(self) -> str:
        """Return the first active trip reason, or '' if the breaker is closed."""
        if self.session_ceiling_cents > 0 and self.spent_cents >= self.session_ceiling_cents:
            return (
                f"session spend {self.spent_cents} cents reached ceiling "
                f"{self.session_ceiling_cents} cents"
            )
        if self.max_consecutive_failures > 0 and self.consecutive_failures >= self.max_consecutive_failures:
            return (
                f"{self.consecutive_failures} consecutive audit failures "
                f"(limit {self.max_consecutive_failures})"
            )
        if self.roi_floor > 0 and self.spent_cents >= max(1, self.roi_grace_spend_cents):
            roi = self.revenue_cents / self.spent_cents if self.spent_cents else 0.0
            if roi < self.roi_floor:
                return (
                    f"ROI {roi:.2f} below floor {self.roi_floor:.2f} after "
                    f"{self.spent_cents} cents spent for {self.revenue_cents} cents revenue"
                )
        return ""

    def check(self, *, next_spend_cents: int = 0) -> None:
        """
        Raise CircuitBreakerTrippedError if the breaker is (or, with `next_spend_cents`,
        would be) tripped. Call this BEFORE funding the next task in a loop.
        """
        # Look-ahead: would committing the next task push cumulative spend to the ceiling?
        if (
            self.session_ceiling_cents > 0
            and next_spend_cents > 0
            and self.spent_cents + next_spend_cents > self.session_ceiling_cents
        ):
            reason = (
                f"next task ~{next_spend_cents} cents would push session spend past ceiling "
                f"{self.session_ceiling_cents} cents (already {self.spent_cents})"
            )
            self._trip(reason)
        reason = self._trip_reason()
        if reason:
            self._trip(reason)

    def _trip(self, reason: str) -> None:
        self._tripped_reason = reason
        logger.critical("GOVERNANCE CFO: circuit breaker TRIPPED — %s. Halting session.", reason)
        raise CircuitBreakerTrippedError(
            f"CFO circuit breaker: {reason}", reason=reason, spent_cents=self.spent_cents
        )

    def reset(self) -> None:
        """Clear all session counters (start a fresh session)."""
        self.spent_cents = 0
        self.revenue_cents = 0
        self.consecutive_failures = 0
        self._tripped_reason = ""

    def status(self) -> dict[str, object]:
        """Snapshot for dashboards / logging."""
        roi = self.revenue_cents / self.spent_cents if self.spent_cents else None
        return {
            "spent_cents": self.spent_cents,
            "revenue_cents": self.revenue_cents,
            "consecutive_failures": self.consecutive_failures,
            "roi": round(roi, 3) if roi is not None else None,
            "session_ceiling_cents": self.session_ceiling_cents,
            "tripped": self.is_tripped,
            "trip_reason": self._trip_reason(),
        }
