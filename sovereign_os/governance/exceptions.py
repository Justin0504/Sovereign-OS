"""Governance-layer exceptions."""


class FiscalInsolvencyError(Exception):
    """
    Raised when the CFO denies budget (e.g. insufficient balance, daily cap exceeded).

    The mission must gracefully abort when this is raised.
    """

    def __init__(self, message: str, *, balance_cents: int | None = None, requested_cents: int | None = None) -> None:
        super().__init__(message)
        self.balance_cents = balance_cents
        self.requested_cents = requested_cents


class AuditFailureError(Exception):
    """
    Raised when a task fails verification against Charter KPIs.
    Optionally trigger RetryStrategy or abort the mission.
    """

    def __init__(self, task_id: str, reason: str, report: object | None = None) -> None:
        super().__init__(f"Task [{task_id}] failed verification: {reason}")
        self.task_id = task_id
        self.reason = reason
        self.report = report


class HumanApprovalRequiredError(Exception):
    """
    Raised when the compliance hook returns REQUEST_HUMAN_APPROVAL for a spend (e.g. above threshold).
    Caller can leave the job pending and surface the message in the UI.
    """

    def __init__(self, message: str, *, amount_cents: int = 0, task_id: str = "") -> None:
        super().__init__(message)
        self.amount_cents = amount_cents
        self.task_id = task_id


class UnprofitableJobError(Exception):
    """
    Raised when the CFO rejects a job because estimated cost would exceed allowed share of job revenue
    (unit economics: minimum margin not met). Top-company practice: do not accept deals below margin floor.
    """

    def __init__(
        self,
        message: str,
        *,
        job_revenue_cents: int = 0,
        estimated_cost_cents: int = 0,
        min_margin_ratio: float = 0.0,
    ) -> None:
        super().__init__(message)
        self.job_revenue_cents = job_revenue_cents
        self.estimated_cost_cents = estimated_cost_cents
        self.min_margin_ratio = min_margin_ratio
