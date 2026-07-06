"""Tests for the AgentBackend abstraction (native / external CLI coding agents)."""

import asyncio

import pytest

from sovereign_os.agents.base import TaskInput
from sovereign_os.agents.code_workers import CodeAssistantWorker
from sovereign_os.llm.agent_backend import (
    CLIAgentBackend,
    _extract_text,
    build_backend,
    resolve_backend_name,
)


# ------------------------------------------------------------- backend selection
def test_resolve_backend_name_precedence(monkeypatch):
    monkeypatch.delenv("SOVEREIGN_AGENT_BACKEND", raising=False)
    monkeypatch.delenv("SOVEREIGN_BACKEND_CODING", raising=False)
    assert resolve_backend_name("code_assistant") == "native"
    monkeypatch.setenv("SOVEREIGN_AGENT_BACKEND", "claude-code")
    assert resolve_backend_name("code_assistant") == "claude-code"
    monkeypatch.setenv("SOVEREIGN_BACKEND_CODING", "codex")  # per-category wins
    assert resolve_backend_name("code_assistant") == "codex"


def test_build_backend_native_is_none(monkeypatch):
    assert build_backend("native") is None
    assert build_backend("") is None


def test_build_backend_known_agent(monkeypatch):
    monkeypatch.delenv("SOVEREIGN_BACKEND_CMD", raising=False)
    b = build_backend("claude-code")
    assert b.backend_id == "claude-code" and b.cmd[0] == "claude" and b.prompt_via == "stdin"
    b2 = build_backend("codex")
    assert b2.cmd[:2] == ["codex", "exec"] and b2.prompt_via == "arg"


def test_build_backend_cmd_override(monkeypatch):
    monkeypatch.setenv("SOVEREIGN_BACKEND_CMD", '["my-agent", "--run"]')
    monkeypatch.setenv("SOVEREIGN_BACKEND_PROMPT_VIA", "arg")
    b = build_backend("custom-thing")  # unknown name but CMD override provides it
    assert b.cmd == ["my-agent", "--run"] and b.prompt_via == "arg"
    monkeypatch.setenv("SOVEREIGN_BACKEND_CMD", "shell-agent --go")  # shell string form
    assert build_backend("x").cmd == ["shell-agent", "--go"]


def test_build_backend_unknown_without_override_is_none(monkeypatch):
    monkeypatch.delenv("SOVEREIGN_BACKEND_CMD", raising=False)
    assert build_backend("no-such-agent") is None


def test_enabled_gate(monkeypatch):
    monkeypatch.delenv("SOVEREIGN_AGENT_BACKEND_ENABLED", raising=False)
    assert build_backend("codex").enabled is False
    monkeypatch.setenv("SOVEREIGN_AGENT_BACKEND_ENABLED", "true")
    assert build_backend("codex").enabled is True


# ----------------------------------------------------------- execute_task
def test_dry_run_does_not_spawn():
    ran = {"n": 0}

    async def runner(cmd, cwd, stdin, timeout):
        ran["n"] += 1
        return {"rc": 0, "stdout": "x"}

    b = CLIAgentBackend("codex", ["codex", "exec"], enabled=False, runner=runner)
    r = asyncio.run(b.execute_task(description="fix", cwd="/tmp/r"))
    assert r["success"] is False and r["metadata"]["dry_run"] is True and ran["n"] == 0


def test_arg_mode_appends_prompt_and_parses_json():
    cap = {}

    async def runner(cmd, cwd, stdin, timeout):
        cap.update(cmd=cmd, cwd=cwd, stdin=stdin)
        return {"rc": 0, "stdout": '{"result": "Done: fixed and tests pass."}', "stderr": ""}

    b = CLIAgentBackend("codex", ["codex", "exec"], prompt_via="arg", enabled=True, runner=runner)
    r = asyncio.run(b.execute_task(description="fix null deref", system_prompt="engineer", cwd="/tmp/r"))
    assert r["success"] is True and r["output"] == "Done: fixed and tests pass."
    assert cap["cmd"][:2] == ["codex", "exec"] and "fix null deref" in cap["cmd"][-1]
    assert cap["stdin"] == ""  # arg mode: nothing on stdin


def test_stdin_mode_pipes_prompt():
    cap = {}

    async def runner(cmd, cwd, stdin, timeout):
        cap.update(cmd=cmd, stdin=stdin)
        return {"rc": 0, "stdout": "plain summary", "stderr": ""}

    b = CLIAgentBackend("claude-code", ["claude", "-p"], prompt_via="stdin", enabled=True, runner=runner)
    r = asyncio.run(b.execute_task(description="do the thing", cwd="/tmp/r"))
    assert r["output"] == "plain summary" and cap["cmd"] == ["claude", "-p"]
    assert "do the thing" in cap["stdin"]  # stdin mode: prompt piped in


def test_nonzero_rc_is_failure():
    async def runner(cmd, cwd, stdin, timeout):
        return {"rc": 1, "stdout": "", "stderr": "boom"}

    b = CLIAgentBackend("codex", ["codex"], enabled=True, runner=runner)
    r = asyncio.run(b.execute_task(description="x", cwd="/tmp"))
    assert r["success"] is False and "boom" in r["output"]


def test_extract_text_variants():
    assert _extract_text('{"result": "hi"}') == "hi"
    assert _extract_text('log line\n{"output": "final answer"}') == "final answer"
    assert _extract_text("no json here") == ""
    assert _extract_text("") == ""


# ------------------------------------------------------ worker delegation wiring
def test_worker_delegates_when_backend_configured(monkeypatch):
    monkeypatch.setenv("SOVEREIGN_AGENT_BACKEND", "codex")
    monkeypatch.setenv("SOVEREIGN_AGENT_BACKEND_ENABLED", "true")

    async def runner(cmd, cwd, stdin, timeout):
        return {"rc": 0, "stdout": '{"result": "delegated fix complete"}', "stderr": ""}

    monkeypatch.setattr("sovereign_os.llm.agent_backend._default_runner", runner)
    w = CodeAssistantWorker("coder", "engineer", llm=None)
    task = TaskInput(task_id="t1", description="Fix the bug", context={"workspace_root": "/tmp/repo"})
    r = asyncio.run(w.execute(task))
    assert r.metadata.get("backend") == "codex"
    assert r.success is True and "delegated fix complete" in r.output


def test_worker_native_when_no_backend(monkeypatch):
    monkeypatch.delenv("SOVEREIGN_AGENT_BACKEND", raising=False)
    monkeypatch.delenv("SOVEREIGN_BACKEND_CODING", raising=False)
    w = CodeAssistantWorker("coder", "engineer", llm=None)  # no LLM -> native echo path
    r = asyncio.run(w.execute(TaskInput(task_id="t2", description="explain",
                                        context={"workspace_root": "/tmp/repo"})))
    assert r.metadata.get("backend") is None
    assert r.metadata.get("worker") == "CodeAssistantWorker"


def test_worker_native_when_no_workspace(monkeypatch):
    monkeypatch.setenv("SOVEREIGN_AGENT_BACKEND", "codex")
    w = CodeAssistantWorker("coder", "engineer", llm=None)
    r = asyncio.run(w.execute(TaskInput(task_id="t3", description="explain")))  # no workspace_root
    assert r.metadata.get("backend") is None  # fell through to native
