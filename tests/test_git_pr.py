"""Tests for the submit_pr connector (dry-run default + injected runner)."""

from sovereign_os.connectors import dispatch
from sovereign_os.connectors.git_pr import submit_pr


def test_requires_branch_and_title(tmp_path):
    assert "error" in submit_pr(str(tmp_path), branch="", title="x", runner=lambda c, w: (0, ""))


def test_dry_run_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("SOVEREIGN_CODE_EXEC_ENABLED", raising=False)
    r = submit_pr(str(tmp_path), branch="fix/bug", title="Fix the bug")
    assert r["dry_run"] is True and r["submitted"] is False
    assert any("gh pr create" in s for s in r["steps"])


def test_injected_runner_runs_full_sequence(tmp_path):
    calls = []
    def runner(cmd, cwd):
        calls.append(cmd)
        if cmd[:3] == ["gh", "pr", "create"]:
            return 0, "https://github.com/org/repo/pull/42\n"
        return 0, ""
    r = submit_pr(str(tmp_path), branch="fix/x", title="Fix X", body="details", runner=runner)
    assert r["submitted"] is True and r["pr_url"] == "https://github.com/org/repo/pull/42"
    # branch -> add -> commit -> push -> pr
    assert calls[0][:2] == ["git", "checkout"] and calls[-1][:3] == ["gh", "pr", "create"]


def test_stops_on_failed_step(tmp_path):
    def runner(cmd, cwd):
        return (1, "merge conflict") if cmd[0] == "git" and cmd[1] == "commit" else (0, "")
    r = submit_pr(str(tmp_path), branch="b", title="t", runner=runner)
    assert r["submitted"] is False and "commit" in r["failed_at"]


def test_dispatch_submit_pr_dry_run(tmp_path, monkeypatch):
    monkeypatch.delenv("SOVEREIGN_CODE_EXEC_ENABLED", raising=False)
    r = dispatch("submit_pr", root=str(tmp_path), branch="b", title="t")
    assert r["dry_run"] is True
