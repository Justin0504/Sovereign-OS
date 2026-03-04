"""
Auditor base: AuditReport model and abstract BaseAuditor for persistence and extension.
"""

from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Annotated

from pydantic import BaseModel, Field


class AuditReport(BaseModel):
    """
    Result of a single task audit: Judge LLM output plus identifiers for persistence.

    Persisted for audit trail; used to update TrustScore and trigger retry/abort.
    """

    task_id: Annotated[str, Field(min_length=1)]
    kpi_name: str = ""
    passed: bool = False
    score: float = 0.0
    reason: str = ""
    suggested_fix: str = ""
    timestamp_utc: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    model_config = {}


class BaseAuditor(ABC):
    """Abstract auditor: evaluate task output against a verification prompt."""

    @abstractmethod
    async def evaluate(self, task_id: str, task_output: str, verification_prompt: str, kpi_name: str) -> AuditReport:
        """Run evaluation (e.g. Judge LLM) and return AuditReport."""
        ...
