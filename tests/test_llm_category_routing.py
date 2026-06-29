"""LLM-path category routing: generic planner skills upgrade to top-tier workers."""

import pytest

from sovereign_os.governance.strategist import PlannedTask, Strategist, TaskPlan
from sovereign_os.models.charter import Charter


class FakeLLM:
    """Returns a plan whose skills are generic, as a weak planner might."""
    def __init__(self, tasks):
        self._tasks = tasks
    async def plan_from_goal(self, goal, charter):
        return TaskPlan(goal_summary=goal, tasks=[
            PlannedTask(task_id=f"task-{i+1}", description=d, dependencies=[],
                        required_skill=s, estimated_token_budget=2000, priority="high")
            for i, (d, s) in enumerate(self._tasks)
        ])


@pytest.mark.asyncio
async def test_generic_skills_upgrade_to_specialists():
    fake = FakeLLM([
        ("Design a clean settings page with dark mode", "summarize"),   # generic -> design_brief
        ("Analyze this churn CSV and find drivers", "assistant_chat"),  # generic -> data_analysis
        ("Refactor and fix the failing auth module", "general"),         # generic -> code_assistant
    ])
    s = Strategist(Charter(mission="m"), llm_client=fake)
    plan = await s.create_plan("multi")
    skills = [t.required_skill for t in plan.tasks]
    assert skills == ["design_brief", "data_analysis", "code_assistant"]


@pytest.mark.asyncio
async def test_recognized_skills_are_preserved():
    fake = FakeLLM([
        ("Research the BNPL landscape", "research"),     # already specific -> keep
        ("Fix a bug in the parser", "code_assistant"),   # already specific -> keep
    ])
    s = Strategist(Charter(mission="m"), llm_client=fake)
    plan = await s.create_plan("multi")
    assert [t.required_skill for t in plan.tasks] == ["research", "code_assistant"]
