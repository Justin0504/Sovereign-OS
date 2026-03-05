"""
Metadata schema for Corporate Memory entries.
Each entry includes timestamp, agent_id, audit_score, kpi_target, raw_output.
Reflections add failure_reason and corrected_logic.
"""

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, Field


class MemoryEntry(BaseModel):
    """
    One persisted memory: successful task pattern or reflection.
    Stored in vector DB with metadata for filtering and retrieval.
    """

    timestamp: datetime
    agent_id: Annotated[str, Field(min_length=1)]
    audit_score: float = 0.0
    kpi_target: str = ""
    raw_output: str = ""
    lessons_learned: str = ""
    is_reflection: bool = False  # True = high priority (from audit failure)

    def to_document_text(self) -> str:
        """Text to embed: task/outcome + lessons so similar tasks can be retrieved."""
        if self.is_reflection:
            return f"Reflection. Lessons: {self.lessons_learned}"
        return f"Task outcome (score={self.audit_score}). Lessons: {self.lessons_learned}".strip()


class ReflectionObject(BaseModel):
    """
    Generated when an audit fails: reason + corrected logic.
    Persisted with high priority so the mistake is not repeated.
    """

    failure_reason: str = ""
    corrected_logic: str = ""
    task_id: str = ""
    agent_id: str = ""
    kpi_name: str = ""
    audit_score: float = 0.0
    raw_output: str = ""

    def to_lessons_learned(self) -> str:
        """Single text for embedding: avoid this failure by applying corrected logic."""
        return (
            f"Failure: {self.failure_reason}. "
            f"Corrected approach: {self.corrected_logic}"
        ).strip()
