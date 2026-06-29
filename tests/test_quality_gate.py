"""Tests for the quality gate: worker revise loop + multi-criteria rubric audit."""

import json
import pytest

from sovereign_os.agents.base import TaskInput
from sovereign_os.agents.specialist_workers import DesignBriefWorker


class FakeLLM:
    """Records turns; returns canned text. model_name + _last_usage like the real client."""
    model_name = "fake"
    def __init__(self, replies):
        self.replies = list(replies)
        self.turns = 0
        self._last_usage = {"input_tokens": 10, "output_tokens": 20}
    async def chat(self, messages):
        self.turns += 1
        return self.replies[min(self.turns - 1, len(self.replies) - 1)]


@pytest.mark.asyncio
async def test_revise_runs_two_turns_and_returns_final():
    llm = FakeLLM(["DRAFT v1", "FINAL improved"])
    w = DesignBriefWorker(agent_id="d1", system_prompt="", llm=llm)
    r = await w.execute(TaskInput(task_id="t1", description="Design a page", context={"revise": "1"}))
    assert r.success and r.output == "FINAL improved"
    assert llm.turns == 2  # draft + critique/revise


@pytest.mark.asyncio
async def test_no_revise_is_single_turn():
    llm = FakeLLM(["DRAFT only"])
    w = DesignBriefWorker(agent_id="d1", system_prompt="", llm=llm)
    r = await w.execute(TaskInput(task_id="t1", description="Design a page"))
    assert r.output == "DRAFT only" and llm.turns == 1


@pytest.mark.asyncio
async def test_rubric_audit_averages_sub_scores():
    from sovereign_os.auditor.review_engine import JudgeLLM

    class JudgeClient:
        model_name = "judge"
        async def chat(self, messages):
            # high-value path requests a rubric
            return json.dumps({"relevance": 0.9, "completeness": 0.8, "correctness": 1.0,
                               "safety": 1.0, "reason": "solid", "suggested_fix": ""})
    j = JudgeLLM(client=JudgeClient())
    rep = await j.evaluate("t1", "a real deliverable", "Does it satisfy?", "kpi", min_score=0.7)
    assert rep.sub_scores == {"relevance": 0.9, "completeness": 0.8, "correctness": 1.0, "safety": 1.0}
    assert abs(rep.score - 0.925) < 1e-6 and rep.passed is True
    assert rep.proof_hash  # still hashes (sub_scores excluded from canonical)


@pytest.mark.asyncio
async def test_rubric_audit_fails_below_bar():
    from sovereign_os.auditor.review_engine import JudgeLLM

    class JudgeClient:
        model_name = "judge"
        async def chat(self, messages):
            return json.dumps({"relevance": 0.5, "completeness": 0.4, "correctness": 0.6,
                               "safety": 1.0, "reason": "gaps", "suggested_fix": "cover X"})
    j = JudgeLLM(client=JudgeClient())
    rep = await j.evaluate("t1", "weak", "Q?", "kpi", min_score=0.7)
    assert rep.score < 0.7 and rep.passed is False and rep.suggested_fix
