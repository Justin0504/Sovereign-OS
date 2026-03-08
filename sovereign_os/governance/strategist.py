"""
Strategist: The CEO Mind — goal parsing and task decomposition.

Uses a Reasoning Model to map a high-level goal against Charter.core_competencies
and produce a TaskPlan (tasks with dependencies, skills, estimated token budget).
"""

import json
import logging
from typing import Annotated

from pydantic import BaseModel, Field

from sovereign_os.llm.providers import ChatLLM, create_llm_client
from sovereign_os.models.charter import Charter

logger = logging.getLogger(__name__)


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
    original_goal: str | None = None  # Set by engine so workers receive full client brief


def _normalize_plan_task_ids(plan: TaskPlan) -> TaskPlan:
    """
    Ensure every task has a unique, readable task_id (e.g. task-1-spec_writer, task-2-reply).
    Remaps dependencies so they refer to the new ids. Fixes audit trail showing all 'task-1'.
    """
    if not plan.tasks:
        return plan
    new_ids = [f"task-{i + 1}-{t.required_skill}" for i, t in enumerate(plan.tasks)]
    # Map old task_id at position i -> new_ids[i] (handles LLM returning duplicate "task-1")
    new_tasks = []
    for i, t in enumerate(plan.tasks):
        new_deps = []
        for d in t.dependencies:
            for j, t2 in enumerate(plan.tasks):
                if t2.task_id == d:
                    new_deps.append(new_ids[j])
                    break
        new_deps = list(dict.fromkeys(new_deps))
        new_tasks.append(
            t.model_copy(update={"task_id": new_ids[i], "dependencies": new_deps})
        )
    return plan.model_copy(update={"tasks": new_tasks})


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

    Default Strategist LLM client; actual provider/model are resolved
    via sovereign_os.llm.providers.create_llm_client(role="strategist").
    """

    def __init__(self, *, client: ChatLLM | None = None) -> None:
        self._client = client or create_llm_client("strategist")

    @property
    def model_name(self) -> str:
        return self._client.model_name

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
            from sovereign_os.telemetry.tracer import span_llm
        except ImportError:
            span_llm = lambda *a, **kw: __import__("contextlib").contextmanager(lambda: (yield))()
        with span_llm("strategist.create_plan", model=self.model_name):
            content = await self._client.chat(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ]
            )
        content = content or "{}"
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
        if llm_client is not None:
            self._llm = llm_client
        else:
            # Try to create a default LLM-backed Strategist; if that fails, fall back to stub plan.
            try:
                self._llm = OpenAIStrategistLLM()
            except Exception as e:  # pragma: no cover - optional LLM path
                logger.warning(
                    "GOVERNANCE CEO: No LLM configured for Strategist; "
                    "falling back to single-task plan. (%s)",
                    e,
                )
                self._llm = None

    async def create_plan(self, goal_text: str) -> TaskPlan:
        """
        Produce a TaskPlan for the given goal.

        If an LLM client is configured, uses it; otherwise returns a single
        placeholder task so the pipeline can run without an API.
        """
        if self._llm is not None:
            plan = await self._llm.plan_from_goal(goal_text, self._charter)
            plan = _normalize_plan_task_ids(plan)
            logger.info(
                "GOVERNANCE CEO: Strategic plan produced: %d tasks for goal (summary=%s).",
                len(plan.tasks),
                (plan.goal_summary or goal_text)[:80],
            )
            return plan
        # Fallback: infer skill from goal text so the right worker runs (not always first competency)
        competency_names = [c.name for c in self._charter.core_competencies]
        goal_lower = goal_text.lower()
        skill = "summarize"
        if any(k in goal_lower for k in ("market research", "competitive", "bnpl", "competitor", "landscape", "comparison table")):
            skill = "research" if "research" in competency_names else skill
        elif any(k in goal_lower for k in ("blog", "article", "long-form", "1000-word", "word post", "b2b")):
            skill = "write_article" if "write_article" in competency_names else skill
        elif any(k in goal_lower for k in ("email", "cold outreach", "sequence", "d2c", "subject line")):
            skill = "write_email" if "write_email" in competency_names else skill
        elif any(k in goal_lower for k in ("faq", "tooltip", "documentation", "getting started", "help center")):
            skill = "help_docs" if "help_docs" in competency_names else skill
        elif any(k in goal_lower for k in ("launch", "onboarding", "hero", "landing page", "content pack")):
            skill = "write_post" if "write_post" in competency_names else skill
        elif any(k in goal_lower for k in ("strategy", "sow", "template", "exec summary", "decisions log", "workshop")):
            skill = "spec_writer" if "spec_writer" in competency_names else skill
        elif any(k in goal_lower for k in ("solve", "question", "answer", "step")):
            skill = "solve_problem" if "solve_problem" in competency_names else skill
        if skill not in competency_names:
            skill = competency_names[0] if competency_names else "summarize"
        plan = TaskPlan(
            goal_summary=goal_text[:200],
            tasks=[
                PlannedTask(
                    task_id=f"task-1-{skill}",
                    description=goal_text[:500],
                    dependencies=[],
                    required_skill=skill,
                    estimated_token_budget=4000,
                    priority="high",
                ),
            ],
        )
        logger.info(
            "GOVERNANCE CEO: No LLM configured; using fallback plan with 1 task (skill=%s).",
            skill,
        )
        return plan
