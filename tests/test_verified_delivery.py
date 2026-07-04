"""
Tests for verification-driven coding delivery:
  - BaseWorker.run_with_verified_tools accepts a final only after verification passes
  - CodeAssistantWorker exposes tests_verified and fails success on real test failure
"""

import json

import pytest

from sovereign_os.agents.base import BaseWorker, TaskInput
from sovereign_os.agents.code_workers import CodeAssistantWorker


class _ScriptLLM:
    model_name = "fake"

    def __init__(self, replies):
        self.replies = list(replies)
        self.i = 0
        self._last_usage = {"input_tokens": 1, "output_tokens": 1}

    async def chat(self, messages):
        r = self.replies[min(self.i, len(self.replies) - 1)]
        self.i += 1
        return r


class _Concrete(BaseWorker):
    async def execute(self, task):  # pragma: no cover - not used
        ...


def _final(output):
    return json.dumps({"action": "final", "output": output})


def _tool(name, **args):
    return json.dumps({"action": "tool", "tool": name, "args": args})


# ----------------------------------------------------- run_with_verified_tools
@pytest.mark.asyncio
async def test_final_rejected_until_verifier_passes():
    state = {"passed": False}

    def verifier():
        return state["passed"], ("green" if state["passed"] else "2 failing tests")

    def write_fix(args):
        state["passed"] = True
        return "wrote fix"

    w = _Concrete("c", "")
    w.llm = _ScriptLLM([
        _final("def f(): return 0"),          # premature -> verify fails
        _tool("write_file", relpath="f.py"),   # fixes the cause
        _final("def f(): return 1"),           # -> verify passes
    ])
    out, _usage, log, verified = await w.run_with_verified_tools(
        "sys", "fix f", {"write_file": write_fix}, verifier=verifier, max_steps=8,
    )
    assert verified is True
    assert out == "def f(): return 1"  # returns the FIXED code, not the first attempt
    verify_events = [e for e in log if e["tool"] == "__verify__"]
    assert [e["ok"] for e in verify_events] == [False, True]


@pytest.mark.asyncio
async def test_unverified_after_max_rounds_returns_false():
    w = _Concrete("c2", "")
    w.llm = _ScriptLLM([_final("still broken")])  # always claims done
    out, _u, log, verified = await w.run_with_verified_tools(
        "s", "u", {}, verifier=lambda: (False, "still failing"),
        max_steps=8, max_verify_rounds=3,
    )
    assert verified is False
    assert out == "still broken"  # best attempt preserved
    assert sum(1 for e in log if e["tool"] == "__verify__") == 3


@pytest.mark.asyncio
async def test_skip_verifier_passes_immediately():
    w = _Concrete("c3", "")
    w.llm = _ScriptLLM([_final("code")])
    out, _u, _log, verified = await w.run_with_verified_tools(
        "s", "u", {}, verifier=lambda: (True, "skipped"),
    )
    assert verified is True and out == "code"


@pytest.mark.asyncio
async def test_verifier_exception_is_treated_as_failure():
    def boom():
        raise RuntimeError("test harness crashed")

    w = _Concrete("c4", "")
    w.llm = _ScriptLLM([_final("x")])
    _out, _u, log, verified = await w.run_with_verified_tools(
        "s", "u", {}, verifier=boom, max_steps=4, max_verify_rounds=2,
    )
    assert verified is False
    assert any("verifier error" in e["obs"] for e in log if e["tool"] == "__verify__")


# --------------------------------------------------- CodeAssistantWorker wiring
@pytest.mark.asyncio
async def test_code_worker_marks_success_false_on_real_test_failure(monkeypatch):
    # Make run_tests actually "run" and fail.
    def fake_dispatch(name, **kwargs):
        if name == "run_tests":
            return {"dry_run": False, "rc": 1, "passed": False, "output": "1 failed"}
        return {"dry_run": False}

    monkeypatch.setattr("sovereign_os.connectors.dispatch", fake_dispatch)
    w = CodeAssistantWorker("coder", "")
    w.llm = _ScriptLLM([_final("def f(): return 0")])  # claims done, but tests fail
    task = TaskInput(task_id="t1", description="fix the bug",
                     context={"use_tools": "true", "workspace_root": "/tmp/repo"})
    r = await w.execute(task)
    assert r.metadata.get("tests_verified") is False
    assert r.success is False  # broken code must not pass as a success


@pytest.mark.asyncio
async def test_code_worker_dry_run_does_not_block(monkeypatch):
    def fake_dispatch(name, **kwargs):
        return {"dry_run": True}  # execution disabled

    monkeypatch.setattr("sovereign_os.connectors.dispatch", fake_dispatch)
    w = CodeAssistantWorker("coder", "")
    w.llm = _ScriptLLM([_final("def f(): return 1")])
    task = TaskInput(task_id="t2", description="fix",
                     context={"use_tools": "true", "workspace_root": "/tmp/repo"})
    r = await w.execute(task)
    assert r.metadata.get("tests_verified") is True  # skip => not blocked
    assert r.success is True
