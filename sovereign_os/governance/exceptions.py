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
