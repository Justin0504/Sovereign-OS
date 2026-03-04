"""
SovereignAuth: Permission and TrustScore management.

Agents start in a restricted sandbox; permissions are granted dynamically
based on AuditHistory and TrustScore. Each capability has a minimum TrustScore threshold.
"""

import logging
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class PermissionDeniedError(Exception):
    """Raised when an agent's TrustScore does not meet the threshold for a capability."""

    def __init__(self, agent_id: str, capability: "Capability", score: int, threshold: int) -> None:
        self.agent_id = agent_id
        self.capability = capability
        self.score = score
        self.threshold = threshold
        super().__init__(f"Agent [{agent_id}] denied {capability.value} (score={score}, threshold={threshold})")


class Capability(str, Enum):
    """Permissions an agent may request. Thresholds are enforced by TrustScore."""

    READ_FILES = "read_files"
    WRITE_FILES = "write_files"
    EXECUTE_SHELL = "execute_shell"
    SPEND_USD = "spend_usd"
    CALL_EXTERNAL_API = "call_external_api"


# Default minimum TrustScore (0–100) required for each capability.
# Stricter capabilities require higher scores.
DEFAULT_CAPABILITY_THRESHOLDS: dict[Capability, int] = {
    Capability.READ_FILES: 10,
    Capability.WRITE_FILES: 40,
    Capability.EXECUTE_SHELL: 60,
    Capability.SPEND_USD: 80,
    Capability.CALL_EXTERNAL_API: 50,
}

DEFAULT_BASE_TRUST_SCORE = 50
DEFAULT_AUDIT_SUCCESS_DELTA = 5
DEFAULT_AUDIT_FAILURE_DELTA = -15
DEFAULT_BUDGET_OVERRUN_DELTA = -10


class SovereignAuth:
    """
    Dynamic guardrail: permissions granted only when agent's TrustScore
    meets the threshold for the requested capability.
    """

    def __init__(
        self,
        *,
        base_trust_score: int = DEFAULT_BASE_TRUST_SCORE,
        capability_thresholds: dict[Capability, int] | None = None,
        audit_success_delta: int = DEFAULT_AUDIT_SUCCESS_DELTA,
        audit_failure_delta: int = DEFAULT_AUDIT_FAILURE_DELTA,
        budget_overrun_delta: int = DEFAULT_BUDGET_OVERRUN_DELTA,
    ) -> None:
        self._scores: dict[str, int] = {}
        self._base = max(0, min(100, base_trust_score))
        self._thresholds = capability_thresholds or dict(DEFAULT_CAPABILITY_THRESHOLDS)
        self._audit_success_delta = audit_success_delta
        self._audit_failure_delta = audit_failure_delta
        self._budget_overrun_delta = budget_overrun_delta

    def _get_score(self, agent_id: str) -> int:
        return self._scores.get(agent_id, self._base)

    def _set_score(self, agent_id: str, value: int) -> None:
        self._scores[agent_id] = max(0, min(100, value))

    def get_trust_score(self, agent_id: str) -> int:
        """Return current TrustScore for the agent (0–100)."""
        return self._get_score(agent_id)

    def get_threshold(self, capability: Capability) -> int:
        """Return the minimum TrustScore required for the capability."""
        return self._thresholds.get(capability, 100)

    def check_permission(self, agent_id: str, capability: Capability) -> bool:
        """
        Return True only if the agent's TrustScore meets the threshold for this capability.
        Logs request and GRANTED/DENIED outcome.
        """
        score = self._get_score(agent_id)
        threshold = self._thresholds.get(capability, 100)
        granted = score >= threshold
        logger.info(
            "AGENTS AUTH: Agent [%s] requesting %s permission... [%s] (score=%d, threshold=%d)",
            agent_id,
            capability.value,
            "GRANTED" if granted else "DENIED",
            score,
            threshold,
        )
        return granted

    def record_audit_success(self, agent_id: str) -> None:
        """Increase TrustScore after Auditor verifies task success."""
        old = self._get_score(agent_id)
        self._set_score(agent_id, old + self._audit_success_delta)
        logger.debug("AGENTS AUTH: Agent [%s] audit success; trust %d -> %d", agent_id, old, self._get_score(agent_id))

    def record_audit_failure(self, agent_id: str) -> None:
        """Decrease TrustScore after audit failure."""
        old = self._get_score(agent_id)
        self._set_score(agent_id, old + self._audit_failure_delta)
        logger.debug("AGENTS AUTH: Agent [%s] audit failure; trust %d -> %d", agent_id, old, self._get_score(agent_id))

    def record_budget_overrun(self, agent_id: str) -> None:
        """Decrease TrustScore after budget overrun."""
        old = self._get_score(agent_id)
        self._set_score(agent_id, old + self._budget_overrun_delta)
        logger.debug("AGENTS AUTH: Agent [%s] budget overrun; trust %d -> %d", agent_id, old, self._get_score(agent_id))
