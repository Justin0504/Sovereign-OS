"""
Strategist: The CEO Mind — goal parsing and task decomposition.

Uses a Reasoning Model to map a high-level goal against Charter.core_competencies
and produce a TaskPlan (tasks with dependencies, skills, estimated token budget).
"""

import json
import logging
from typing import Annotated

from pydantic import BaseModel, Field

from sovereign_os.models.charter import Charter

logger = logging.getLogger(__name__)

try:
    from openai import AsyncOpenAI
except ImportError:
    AsyncOpenAI = None  # type: ignore[misc, assignment]


# ---------------------------------------------------------------------------
# Task plan models
# ---------------------------------------------------------------------------


class PlannedTask(BaseModel):
    """A single task in the strategic plan."""

    task_id: Annotated[str, Field(min_length=1)]
    description: str = ""
    dependencies: list[str] = Field(default_factory=list)  # task_ids that must complete first
    required_skill: Annotated[str, Field(min_length=1)]  # Maps to Charter.core_competencies name
    estimated_token_budget: Annotated[int, Field(ge=0)] = 0
    priority: Annotated[str, Field(description="high | low")] = "low"


class TaskPlan(BaseModel):
    """Output of the CEO: ordered tasks with dependencies and token budgets."""

    goal_summary: str = ""
    tasks: list[PlannedTask] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# LLM client protocol (async, injectable)
# ---------------------------------------------------------------------------


class StrategistLLMProtocol:
    """Async interface for the Strategist to call a Reasoning Model."""

    async def plan_from_goal(self, goal: str, charter: Charter) -> TaskPlan:
        """Produce a TaskPlan from a high-level goal and the entity Charter."""
        ...


class OpenAIStrategistLLM(StrategistLLMProtocol):
    """
    Concrete LLM client using OpenAI API (GPT-4o / o1-preview).

    Requires: pip install openai
    """

    def __init__(self, *, api_key: str | None = None, model: str = "gpt-4o") -> None:
        if AsyncOpenAI is None:
            raise ImportError("openai package is required; install with: pip install openai")
        self._client = AsyncOpenAI(api_key=api_key)
        self._model = model

    async def plan_from_goal(self, goal: str, charter: Charter) -> TaskPlan:
        competencies_text = "\n".join(
            f"- {c.name}: {c.description} (priority {c.priority})"
            for c in charter.core_competencies
        )
        system = (
            "You are a CEO strategist. Given a high-level goal and the entity's core competencies, "
            "output a JSON object with: goal_summary (string), tasks (array of objects with "
            "task_id, description, dependencies [list of task_ids], required_skill (must match a competency name), "
            "estimated_token_budget (int), priority (high or low)). No markdown, only valid JSON."
        )
        user = (
            f"Goal: {goal}\n\nCore competencies:\n{competencies_text}\n\n"
            "Return only the JSON object for the task plan."
        )
        try:
            from sovereign_os.telemetry.tracer import span_llm, record_llm_tokens
        except ImportError:
            span_llm = lambda *a, **kw: __import__("contextlib").contextmanager(lambda: (yield))()
            record_llm_tokens = lambda *a, **k: None
        with span_llm("strategist.create_plan", model=self._model):
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            )
        usage = getattr(response, "usage", None)
        if usage:
            record_llm_tokens(
                self._model,
                getattr(usage, "prompt_tokens", 0) or 0,
                getattr(usage, "completion_tokens", 0) or 0,
            )
        content = response.choices[0].message.content or "{}"
        content = content.strip().removeprefix("```json").removeprefix("```").strip().removesuffix("```").strip()
        data = json.loads(content)
        return TaskPlan.model_validate(data)


# ---------------------------------------------------------------------------
# Strategist
# ---------------------------------------------------------------------------


class Strategist:
    """
    CEO Mind: parses goal, maps to competencies, outputs TaskPlan.

    Uses an injectable async LLM client (Reasoning Model) to produce the plan.
    """

    def __init__(self, charter: Charter, *, llm_client: StrategistLLMProtocol | None = None) -> None:
        self._charter = charter
        self._llm = llm_client

    async def create_plan(self, goal_text: str) -> TaskPlan:
        """
        Produce a TaskPlan for the given goal.

        If an LLM client is configured, uses it; otherwise returns a single
        placeholder task so the pipeline can run without an API.
        """
        if self._llm is not None:
            plan = await self._llm.plan_from_goal(goal_text, self._charter)
            logger.info(
                "GOVERNANCE CEO: Strategic plan produced: %d tasks for goal (summary=%s).",
                len(plan.tasks),
                (plan.goal_summary or goal_text)[:80],
            )
            return plan
        # Fallback: minimal plan when no LLM is configured (e.g. tests / dry run)
        competency_names = [c.name for c in self._charter.core_competencies]
        skill = competency_names[0] if competency_names else "general"
        plan = TaskPlan(
            goal_summary=goal_text[:200],
            tasks=[
                PlannedTask(
                    task_id="task-1",
                    description=goal_text[:500],
                    dependencies=[],
                    required_skill=skill,
                    estimated_token_budget=2000,
                    priority="high",
                ),
            ],
        )
        logger.info(
            "GOVERNANCE CEO: No LLM configured; using fallback plan with 1 task (skill=%s).",
            skill,
        )
        return plan
