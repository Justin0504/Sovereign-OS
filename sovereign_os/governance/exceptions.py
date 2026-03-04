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
