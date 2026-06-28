"""
Strategist: The CEO Mind — goal parsing and task decomposition.

Uses a Reasoning Model to map a high-level goal against Charter.core_competencies
and produce a TaskPlan (tasks with dependencies, skills, estimated token budget).
"""

import json
import logging
import re
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

    def _resolve_skill(self, text: str, competency_names: list[str]) -> str:
        """Route text -> category skill; defer to a declared competency when the routed skill isn't one."""
        from sovereign_os.agents.categories import route_skill

        skill = route_skill("", text) or "summarize"
        if competency_names and skill not in competency_names:
            skill = competency_names[0]
        return skill

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
        # Fallback: route by task CATEGORY (platform-grounded) to the top-tier worker.
        # A goal that clearly spans multiple categories is split into one task per
        # part (sequential deps); otherwise a single task. Charter competencies still
        # take precedence over the routed skill.
        competency_names = [c.name for c in self._charter.core_competencies]
        parts = _candidate_parts(goal_text)
        routed = [(p, self._resolve_skill(p, competency_names)) for p in parts]
        # Only split when there are >=2 distinct parts AND they need >=2 distinct skills.
        if len(routed) >= 2 and len({s for _, s in routed}) >= 2:
            tasks: list[PlannedTask] = []
            ids = [f"task-{i}-{s}" for i, (_, s) in enumerate(routed, 1)]
            for i, (part, skill) in enumerate(routed, 1):
                tasks.append(PlannedTask(
                    task_id=ids[i - 1],
                    description=part[:500],
                    dependencies=([ids[i - 2]] if i > 1 else []),
                    required_skill=skill,
                    estimated_token_budget=4000,
                    priority="high",
                ))
            plan = TaskPlan(goal_summary=goal_text[:200], tasks=tasks)
            logger.info("GOVERNANCE CEO: No LLM; multi-part fallback plan with %d tasks.", len(tasks))
            plan.original_goal = goal_text
            return _normalize_plan_task_ids(plan)

        skill = routed[0][1]
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


_MARKER_RE = re.compile(r"^\s*(?:\d+[.)]|[-*•])\s+")


def _strip_marker(line: str) -> str:
    return _MARKER_RE.sub("", line).strip()


def _candidate_parts(goal: str) -> list[str]:
    """
    Split a goal into distinct parts ONLY when it clearly spans several asks:
    multiple non-empty lines, a numbered/bulleted list, or substantial ' and '
    clauses. Otherwise returns [goal] (single task). Capped at 6 parts.
    """
    goal = goal or ""
    lines = [l.strip() for l in goal.splitlines() if l.strip()]
    if len(lines) > 1:
        return [_strip_marker(l) for l in lines][:6]
    items = re.findall(r"(?m)^\s*(?:\d+[.)]|[-*•])\s+(.+)$", goal)
    if len(items) >= 2:
        return [i.strip() for i in items][:6]
    if re.search(r"\sand\s", goal):
        parts = [p.strip() for p in re.split(r"\s+and\s+", goal) if p.strip()]
        if len(parts) >= 2 and all(len(p) >= 12 for p in parts):
            return parts[:6]
    return [goal]
