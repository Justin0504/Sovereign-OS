"""
Auditor base: AuditReport model and abstract BaseAuditor for persistence and extension.
Verifiable audit trail: each report has a proof_hash (SHA-256 of canonical JSON) for integrity.
"""

import hashlib
import json
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Annotated, Any

from pydantic import BaseModel, Field, computed_field


def _audit_report_canonical(report: "AuditReport") -> dict[str, Any]:
    """Canonical dict for hashing (stable key order, no proof_hash)."""
    return {
        "task_id": report.task_id,
        "kpi_name": report.kpi_name,
        "passed": report.passed,
        "score": report.score,
        "reason": report.reason,
        "suggested_fix": report.suggested_fix,
        "timestamp_utc": report.timestamp_utc.isoformat(),
    }


def compute_audit_proof_hash(report: "AuditReport") -> str:
    """SHA-256 of canonical JSON; used for verifiable audit trail."""
    payload = json.dumps(_audit_report_canonical(report), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class AuditReport(BaseModel):
    """
    Result of a single task audit: Judge LLM output plus identifiers for persistence.
    proof_hash: SHA-256 of canonical fields for verifiable audit trail (Phase 6).
    """

    task_id: Annotated[str, Field(min_length=1)]
    kpi_name: str = ""
    passed: bool = False
    score: float = 0.0
    reason: str = ""
    suggested_fix: str = ""
    timestamp_utc: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    model_config = {}

    @computed_field
    @property
    def proof_hash(self) -> str:
        """SHA-256 of canonical fields for verifiable audit trail (computed)."""
        return compute_audit_proof_hash(self)


class BaseAuditor(ABC):
    """Abstract auditor: evaluate task output against a verification prompt."""

    @abstractmethod
    async def evaluate(self, task_id: str, task_output: str, verification_prompt: str, kpi_name: str) -> AuditReport:
        """Run evaluation (e.g. Judge LLM) and return AuditReport."""
        ...
