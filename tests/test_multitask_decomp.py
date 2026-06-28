"""Tests for Strategist multi-part category decomposition (no-LLM fallback)."""

import pytest

from sovereign_os.governance.strategist import Strategist, _candidate_parts
from sovereign_os.models.charter import Charter


def test_candidate_parts_single_vs_multi():
    assert _candidate_parts("Summarize the market.") == ["Summarize the market."]
    assert _candidate_parts("Complete one research task.") == ["Complete one research task."]
    # multi-line
    assert len(_candidate_parts("Write a blog post\nDesign a logo")) == 2
    # ' and ' between substantial asks
    assert len(_candidate_parts("Write a blog post about AI and design a settings page")) == 2
    # numbered list
    assert len(_candidate_parts("1. Research rivals\n2. Write the brief")) == 2


@pytest.mark.asyncio
async def test_multi_category_goal_splits_into_tasks():
    s = Strategist(Charter(mission="m"))  # no LLM, no competencies
    plan = await s.create_plan("Write a blog post about AI and design a settings page")
    assert len(plan.tasks) == 2
    skills = [t.required_skill for t in plan.tasks]
    assert "write_article" in skills and "design_brief" in skills
    # sequential dependency
    assert plan.tasks[1].dependencies == [plan.tasks[0].task_id]


@pytest.mark.asyncio
async def test_single_part_goal_stays_single():
    s = Strategist(Charter(mission="m"))
    plan = await s.create_plan("Fix a bug in the parser")
    assert len(plan.tasks) == 1 and plan.tasks[0].required_skill == "code_assistant"


@pytest.mark.asyncio
async def test_same_category_and_does_not_split():
    # Two research-ish asks joined by 'and' -> same skill -> stays single.
    s = Strategist(Charter(mission="m"))
    plan = await s.create_plan("Research the BNPL market and research the competitors")
    assert len(plan.tasks) == 1
