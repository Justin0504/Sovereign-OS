"""
BaseWorker: Abstract base for all agents. Async execution and Pydantic message passing.
"""

from abc import ABC, abstractmethod
from typing import Annotated

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Message types (Pydantic for type-safe passing between agents)
# ---------------------------------------------------------------------------


class TaskInput(BaseModel):
    """Input passed to a worker for a single task."""

    task_id: Annotated[str, Field(min_length=1)]
    description: str = ""
    required_skill: str = ""
    context: dict[str, str] = Field(default_factory=dict)


class TaskResult(BaseModel):
    """Result returned by a worker for the (upcoming) Auditor to verify."""

    task_id: Annotated[str, Field(min_length=1)]
    success: bool = True
    output: str = ""
    metadata: dict[str, str | int | float | bool] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# BaseWorker
# ---------------------------------------------------------------------------


class BaseWorker(ABC):
    """
    Abstract worker. All agents support async execution and receive
    a system prompt derived from the Charter for mission alignment.
    """

    def __init__(self, agent_id: str, system_prompt: str = "") -> None:
        self.agent_id = agent_id
        self.system_prompt = system_prompt

    @abstractmethod
    async def execute(self, task: TaskInput) -> TaskResult:
        """Run the task asynchronously and return a result for the Auditor."""
        ...


class StubWorker(BaseWorker):
    """Default worker when no implementation is registered; returns placeholder result for Auditor."""

    async def execute(self, task: TaskInput) -> TaskResult:
        return TaskResult(
            task_id=task.task_id,
            success=True,
            output=f"[Stub] Completed: {task.description[:100] or task.task_id}",
            metadata={"worker": "StubWorker"},
        )
