"""
SovereignAuth: Permission and TrustScore management.

Agents start in a restricted sandbox; permissions are granted dynamically
based on AuditHistory and TrustScore. Each capability has a minimum TrustScore threshold.

Beyond the binary capability gate, autonomous USD spend is *graduated*: once an
agent clears the SPEND_USD threshold, its per-task autonomous spend ceiling
scales linearly with TrustScore — so trust earned through passing audits unlocks
larger budgets, and audit failures shrink them. This ties the permission system
directly to the wealth-management (Treasury) system.
"""

from __future__ import annotations

import json
import logging
from enum import Enum
from pathlib import Path
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

# Graduated autonomous spend (cents) granted across the SPEND_USD threshold..100 range.
DEFAULT_AUTONOMOUS_SPEND_MIN_CENTS = 100    # at exactly the threshold ($1.00)
DEFAULT_AUTONOMOUS_SPEND_MAX_CENTS = 5000   # at TrustScore 100 ($50.00)


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
        autonomous_spend_min_cents: int = DEFAULT_AUTONOMOUS_SPEND_MIN_CENTS,
        autonomous_spend_max_cents: int = DEFAULT_AUTONOMOUS_SPEND_MAX_CENTS,
        persist_path: str | Path | None = None,
    ) -> None:
        self._scores: dict[str, int] = {}
        self._base = max(0, min(100, base_trust_score))
        self._thresholds = capability_thresholds or dict(DEFAULT_CAPABILITY_THRESHOLDS)
        self._audit_success_delta = audit_success_delta
        self._audit_failure_delta = audit_failure_delta
        self._budget_overrun_delta = budget_overrun_delta
        self._spend_min_cents = max(0, autonomous_spend_min_cents)
        self._spend_max_cents = max(self._spend_min_cents, autonomous_spend_max_cents)
        # Per-agent audit tallies, for streak/recovery introspection and dashboards.
        self._history: dict[str, dict[str, int]] = {}
        self._path = Path(persist_path) if persist_path else None
        if self._path and self._path.exists():
            self._load()

    # ------------------------------------------------------------------ state
    def _get_score(self, agent_id: str) -> int:
        return self._scores.get(agent_id, self._base)

    def _set_score(self, agent_id: str, value: int) -> None:
        self._scores[agent_id] = max(0, min(100, value))
        self._save()

    def get_trust_score(self, agent_id: str) -> int:
        """Return current TrustScore for the agent (0–100)."""
        return self._get_score(agent_id)

    def get_threshold(self, capability: Capability) -> int:
        """Return the minimum TrustScore required for the capability."""
        return self._thresholds.get(capability, 100)

    # ------------------------------------------------------------- permissions
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

    def max_spend_cents_for(self, agent_id: str) -> int:
        """
        Per-task autonomous spend ceiling (cents), graduated by TrustScore.

        - Below the SPEND_USD threshold: 0 (agent may not spend autonomously).
        - At the threshold: `autonomous_spend_min_cents`.
        - At TrustScore 100: `autonomous_spend_max_cents`.
        - Linear in between.
        """
        score = self._get_score(agent_id)
        threshold = self._thresholds.get(Capability.SPEND_USD, 80)
        if score < threshold:
            return 0
        span = max(1, 100 - threshold)
        frac = min(1.0, max(0.0, (score - threshold) / span))
        return int(self._spend_min_cents + frac * (self._spend_max_cents - self._spend_min_cents))

    def can_spend(self, agent_id: str, amount_cents: int) -> bool:
        """True if the agent both holds SPEND_USD and the amount is within its graduated ceiling."""
        if not self.check_permission(agent_id, Capability.SPEND_USD):
            return False
        ceiling = self.max_spend_cents_for(agent_id)
        ok = amount_cents <= ceiling
        if not ok:
            logger.info(
                "AGENTS AUTH: Agent [%s] spend %d cents exceeds graduated ceiling %d cents.",
                agent_id, amount_cents, ceiling,
            )
        return ok

    # ----------------------------------------------------------- trust updates
    def _bump(self, agent_id: str, delta: int, *, outcome: str) -> None:
        old = self._get_score(agent_id)
        # Update history BEFORE _set_score, which triggers persistence (so both are saved).
        h = self._history.setdefault(agent_id, {"success": 0, "failure": 0, "overrun": 0})
        if outcome in h:
            h[outcome] += 1
        self._set_score(agent_id, old + delta)
        logger.debug(
            "AGENTS AUTH: Agent [%s] %s; trust %d -> %d (delta=%d)",
            agent_id, outcome, old, self._get_score(agent_id), delta,
        )

    def record_audit(self, agent_id: str, *, passed: bool, score: float | None = None) -> None:
        """
        Update TrustScore from an audit outcome, optionally scaled by the audit score.

        When `score` (0.0–1.0) is given, the trust delta is scaled by quality:
        a strong pass (score≈1.0) earns the full success delta; a marginal pass
        earns less; a hard fail (score≈0.0) loses the full failure delta. When
        `score` is None, the flat success/failure deltas are applied (legacy).
        """
        if score is None:
            if passed:
                self._bump(agent_id, self._audit_success_delta, outcome="success")
            else:
                self._bump(agent_id, self._audit_failure_delta, outcome="failure")
            return
        s = min(1.0, max(0.0, float(score)))
        if passed:
            delta = round(self._audit_success_delta * s)
            self._bump(agent_id, delta, outcome="success")
        else:
            # Worse output (lower score) → larger penalty.
            delta = round(self._audit_failure_delta * (1.0 - s))
            self._bump(agent_id, delta, outcome="failure")

    def record_audit_success(self, agent_id: str) -> None:
        """Increase TrustScore after Auditor verifies task success."""
        self._bump(agent_id, self._audit_success_delta, outcome="success")

    def record_audit_failure(self, agent_id: str) -> None:
        """Decrease TrustScore after audit failure."""
        self._bump(agent_id, self._audit_failure_delta, outcome="failure")

    def record_budget_overrun(self, agent_id: str) -> None:
        """Decrease TrustScore after budget overrun."""
        self._bump(agent_id, self._budget_overrun_delta, outcome="overrun")

    # --------------------------------------------------------- introspection
    def history(self, agent_id: str) -> dict[str, int]:
        """Audit tally for an agent: {success, failure, overrun}."""
        return dict(self._history.get(agent_id, {"success": 0, "failure": 0, "overrun": 0}))

    def snapshot(self) -> dict[str, dict[str, Any]]:
        """All known agents with score, spend ceiling, and audit history (for dashboards)."""
        out: dict[str, dict[str, Any]] = {}
        for agent_id in set(self._scores) | set(self._history):
            out[agent_id] = {
                "trust_score": self._get_score(agent_id),
                "max_spend_cents": self.max_spend_cents_for(agent_id),
                "history": self.history(agent_id),
            }
        return out

    # ------------------------------------------------------------ persistence
    def _save(self) -> None:
        if not self._path:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps({"scores": self._scores, "history": self._history}),
                encoding="utf-8",
            )
        except Exception as e:  # pragma: no cover - best-effort persistence
            logger.warning("AGENTS AUTH: failed to persist trust state: %s", e)

    def _load(self) -> None:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))  # type: ignore[union-attr]
            self._scores = {str(k): int(v) for k, v in data.get("scores", {}).items()}
            self._history = {
                str(k): {kk: int(vv) for kk, vv in v.items()}
                for k, v in data.get("history", {}).items()
            }
        except Exception as e:  # pragma: no cover - best-effort load
            logger.warning("AGENTS AUTH: failed to load trust state: %s", e)
