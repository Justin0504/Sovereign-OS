"""
Phase 6b: Compliance hooks (design).

Before sensitive operations (e.g. SPEND_USD above threshold), the engine can call
ComplianceHook.check(action_type, context) -> Allow | Deny | RequestHumanApproval.
"""

from abc import ABC, abstractmethod
from enum import Enum
from typing import Any


class ComplianceResult(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    REQUEST_HUMAN_APPROVAL = "request_human_approval"


class ComplianceHook(ABC):
    """
    Hook called before sensitive actions (e.g. SPEND_USD, publish to external API).
    Integrates with Human-in-the-Loop: REQUEST_HUMAN_APPROVAL can enqueue for dashboard approval.
    """

    @abstractmethod
    def check(self, action_type: str, context: dict[str, Any]) -> ComplianceResult:
        """
        action_type: e.g. "SPEND_USD", "EXTERNAL_API", "SIGN_TX".
        context: e.g. {"amount_cents": 1000, "agent_id": "...", "job_id": 1}.
        """
        ...


class StubComplianceHook(ComplianceHook):
    """Default: allow all. Replace with real logic (thresholds, allowlists) in production."""

    def check(self, action_type: str, context: dict[str, Any]) -> ComplianceResult:
        return ComplianceResult.ALLOW


class ThresholdComplianceHook(ComplianceHook):
    """
    When amount_cents >= spend_threshold_cents, return REQUEST_HUMAN_APPROVAL; otherwise ALLOW.
    Use with Treasury so that high-value task budgets require a second human approval.
    """

    def __init__(self, spend_threshold_cents: int) -> None:
        self.spend_threshold_cents = spend_threshold_cents

    def check(self, action_type: str, context: dict[str, Any]) -> ComplianceResult:
        amount = context.get("amount_cents", 0) or 0
        if amount >= self.spend_threshold_cents:
            return ComplianceResult.REQUEST_HUMAN_APPROVAL
        return ComplianceResult.ALLOW
