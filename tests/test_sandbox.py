"""Tests for sandboxed code execution (no real Docker)."""

import pytest
from sovereign_os.connectors import sandbox
from sovereign_os.connectors.code_workspace import run_tests
from sovereign_os.connectors.sandbox import build_docker_cmd, select_test_runner


def test_build_docker_cmd_isolation():
    argv = build_docker_cmd(["pytest", "-q"], "/work/repo", image="python:3.12-slim")
    s = " ".join(argv)
    assert "docker" in argv[0] and "--rm" in argv
    assert "--network=none" in argv and "--memory=512m" in argv
    assert "-v" in argv and "/work/repo:/work" in argv and "pytest -q" in s


def test_select_runner_subprocess_when_not_requested(monkeypatch):
    monkeypatch.delenv("SOVEREIGN_CODE_SANDBOX", raising=False)
    assert select_test_runner() is sandbox._subprocess_runner


def test_select_runner_refuses_when_docker_missing(monkeypatch):
    monkeypatch.setenv("SOVEREIGN_CODE_SANDBOX", "docker")
    monkeypatch.setattr(sandbox, "sandbox_available", lambda: False)
    refuse = select_test_runner()
    rc, out = refuse(["pytest"], "/x")
    assert rc == -1 and "refused" in out


def test_select_runner_docker_when_available(monkeypatch):
    monkeypatch.setenv("SOVEREIGN_CODE_SANDBOX", "docker")
    monkeypatch.setattr(sandbox, "sandbox_available", lambda: True)
    assert select_test_runner() is sandbox._docker_runner


def test_run_tests_uses_injected_runner(tmp_path, monkeypatch):
    monkeypatch.setenv("SOVEREIGN_CODE_EXEC_ENABLED", "1")
    seen = {}
    def runner(cmd, cwd, timeout=120.0):
        seen["cmd"], seen["cwd"] = cmd, cwd
        return 0, "1 passed"
    r = run_tests(tmp_path, runner=runner)
    assert r["ran"] and r["passed"] and "passed" in r["output"]
    assert seen["cmd"] == ["pytest", "-q"]


def test_run_tests_dry_run_without_exec(tmp_path, monkeypatch):
    monkeypatch.delenv("SOVEREIGN_CODE_EXEC_ENABLED", raising=False)
    assert run_tests(tmp_path)["dry_run"] is True
