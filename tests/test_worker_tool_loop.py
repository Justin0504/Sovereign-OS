"""Tests for the worker tool-use loop (BaseWorker.run_with_tools) + worker wiring."""

import json
import pytest

from sovereign_os.agents.base import TaskInput, _parse_action
from sovereign_os.agents.code_workers import CodeAssistantWorker
from sovereign_os.agents.research_worker import ResearchWorker


class ScriptLLM:
    """Emits a scripted sequence of replies (JSON tool calls + final)."""
    model_name = "fake"
    def __init__(self, replies):
        self.replies = list(replies); self.turns = 0
        self._last_usage = {"input_tokens": 5, "output_tokens": 5}
    async def chat(self, messages):
        self.turns += 1
        return self.replies[min(self.turns - 1, len(self.replies) - 1)]


def test_parse_action_tolerates_fences_and_prose():
    assert _parse_action('```json\n{"action":"final","output":"x"}\n```')["output"] == "x"
    assert _parse_action('sure: {"action":"tool","tool":"web_fetch","args":{"url":"u"}}')["tool"] == "web_fetch"
    assert _parse_action("not json") is None


@pytest.mark.asyncio
async def test_research_worker_uses_web_fetch_then_finalizes(monkeypatch):
    # Tool result is injected via the connectors dispatch (patch web_fetch).
    import sovereign_os.connectors as conn
    monkeypatch.setattr(conn, "dispatch", lambda name, **kw: {"status": 200, "text": "LIVE FACT: market is $9B"} if name == "web_fetch" else {"error": "x"})
    llm = ScriptLLM([
        json.dumps({"action": "tool", "tool": "web_fetch", "args": {"url": "https://x"}}),
        json.dumps({"action": "final", "output": "## Summary\nMarket is $9B (fetched)."}),
    ])
    w = ResearchWorker(agent_id="r1", system_prompt="", llm=llm)
    r = await w.execute(TaskInput(task_id="t1", description="size the market", context={"use_tools": "1"}))
    assert r.success and "9B" in r.output
    assert r.metadata["tool_calls"] == 1 and llm.turns == 2


@pytest.mark.asyncio
async def test_code_worker_reads_repo_via_tool(tmp_path, monkeypatch):
    (tmp_path / "bug.py").write_text("def add(a,b):\n    return a-b  # bug\n")
    llm = ScriptLLM([
        json.dumps({"action": "tool", "tool": "read_file", "args": {"relpath": "bug.py"}}),
        json.dumps({"action": "final", "output": "The bug: add() subtracts; fix to a+b."}),
    ])
    w = CodeAssistantWorker(agent_id="c1", system_prompt="", llm=llm)
    r = await w.execute(TaskInput(task_id="t1", description="find the bug",
                                  context={"use_tools": "1", "workspace_root": str(tmp_path)}))
    assert r.success and "a+b" in r.output and r.metadata["tool_calls"] == 1


@pytest.mark.asyncio
async def test_no_use_tools_is_single_shot():
    llm = ScriptLLM(["plain research output"])
    w = ResearchWorker(agent_id="r1", system_prompt="", llm=llm)
    r = await w.execute(TaskInput(task_id="t1", description="x"))  # no use_tools
    assert r.metadata["tool_calls"] == 0 and llm.turns == 1
