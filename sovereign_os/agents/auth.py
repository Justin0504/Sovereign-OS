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
        # Per-(agent, category) trust: trust is earned per delivery domain, so an
        # agent proven at one category can take its higher-risk work without
        # blanket-trusting it everywhere.
        self._cat_scores: dict[str, dict[str, int]] = {}
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

    def max_spend_cents_for(self, agent_id: str, category: str | None = None) -> int:
        """
        Per-task autonomous spend ceiling (cents), graduated by TrustScore (or
        per-category trust when `category` is given).

        - Below the SPEND_USD threshold: 0 (agent may not spend autonomously).
        - At the threshold: `autonomous_spend_min_cents`.
        - At TrustScore 100: `autonomous_spend_max_cents`.
        - Linear in between.
        """
        score = self.effective_trust(agent_id, category)
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

    def record_audit(self, agent_id: str, *, passed: bool, score: float | None = None, category: str | None = None) -> None:
        """
        Update TrustScore from an audit outcome, optionally scaled by the audit score.

        When `score` (0.0–1.0) is given, the trust delta is scaled by quality:
        a strong pass (score≈1.0) earns the full success delta; a marginal pass
        earns less; a hard fail (score≈0.0) loses the full failure delta. When
        `score` is None, the flat success/failure deltas are applied (legacy).

        When `category` is given, the same delta is also applied to the agent's
        per-category trust, so domain expertise accrues separately from global trust.
        """
        if score is None:
            delta = self._audit_success_delta if passed else self._audit_failure_delta
        elif passed:
            delta = round(self._audit_success_delta * min(1.0, max(0.0, float(score))))
        else:
            delta = round(self._audit_failure_delta * (1.0 - min(1.0, max(0.0, float(score)))))
        self._bump(agent_id, delta, outcome="success" if passed else "failure")
        if category:
            self._bump_category(agent_id, category, delta)

    # ------------------------------------------------------ per-category trust
    def _bump_category(self, agent_id: str, category: str, delta: int) -> None:
        cats = self._cat_scores.setdefault(agent_id, {})
        cur = cats.get(category, self._get_score(agent_id))  # seed from global on first sight
        cats[category] = max(0, min(100, cur + delta))
        self._save()

    def category_trust(self, agent_id: str, category: str) -> int:
        """Per-category trust (seeds from global trust until the agent has category history)."""
        cats = self._cat_scores.get(agent_id, {})
        return cats.get(category, self._get_score(agent_id))

    def effective_trust(self, agent_id: str, category: str | None = None) -> int:
        """Trust to use for a decision: the per-category score when present, else global."""
        return self.category_trust(agent_id, category) if category else self._get_score(agent_id)

    def check_permission_for(self, agent_id: str, capability: Capability, category: str | None = None) -> bool:
        """Like check_permission, but evaluates against per-category trust when a category is given."""
        score = self.effective_trust(agent_id, category)
        threshold = self._thresholds.get(capability, 100)
        granted = score >= threshold
        logger.info(
            "AGENTS AUTH: Agent [%s] requesting %s for category=%s... [%s] (score=%d, threshold=%d)",
            agent_id, capability.value, category or "-", "GRANTED" if granted else "DENIED", score, threshold,
        )
        return granted

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
        for agent_id in set(self._scores) | set(self._history) | set(self._cat_scores):
            out[agent_id] = {
                "trust_score": self._get_score(agent_id),
                "max_spend_cents": self.max_spend_cents_for(agent_id),
                "history": self.history(agent_id),
                "category_trust": dict(self._cat_scores.get(agent_id, {})),
            }
        return out

    # ------------------------------------------------------------ persistence
    def _save(self) -> None:
        if not self._path:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps({"scores": self._scores, "history": self._history, "cat_scores": self._cat_scores}),
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
            self._cat_scores = {
                str(k): {str(kk): int(vv) for kk, vv in v.items()}
                for k, v in data.get("cat_scores", {}).items()
            }
        except Exception as e:  # pragma: no cover - best-effort load
            logger.warning("AGENTS AUTH: failed to load trust state: %s", e)
