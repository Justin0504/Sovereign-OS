"""Tests for TaskBounty PR delivery-back (dry-run default + injected http)."""

from sovereign_os.delivery.taskbounty import deliver_result_to_taskbounty, extract_pr_url


def test_extract_pr_url():
    assert extract_pr_url("Fixed it. PR: https://github.com/org/repo/pull/42 done") == "https://github.com/org/repo/pull/42"
    assert extract_pr_url("no link") == ""


def test_no_bounty_id():
    assert deliver_result_to_taskbounty({"platform": "taskbounty"}, "x", "job-1") is False


def test_dry_run_by_default(monkeypatch):
    monkeypatch.delenv("TASKBOUNTY_LIVE", raising=False)
    ok = deliver_result_to_taskbounty({"platform": "taskbounty", "bounty_id": "tb-1"},
                                      "Opened https://github.com/o/r/pull/7", "job-1")
    assert ok is True  # dry-run flow ran, nothing submitted


def test_live_posts_pr(monkeypatch):
    monkeypatch.setenv("TASKBOUNTY_LIVE", "true")
    monkeypatch.setenv("TASKBOUNTY_API_KEY", "tb_live_x")
    calls = []
    def _post(url, body, headers, timeout):
        calls.append((url, body, headers)); return {"ok": True}
    ok = deliver_result_to_taskbounty({"platform": "taskbounty", "bounty_id": "tb-9"},
                                      "Done: https://github.com/o/r/pull/9", "job-2", post_json=_post)
    assert ok is True and len(calls) == 1
    url, body, headers = calls[0]
    assert url.endswith("/tasks/tb-9/submit")
    assert body["prUrl"] == "https://github.com/o/r/pull/9"
    assert headers["Authorization"] == "Bearer tb_live_x"


def test_live_without_key_skips(monkeypatch):
    monkeypatch.setenv("TASKBOUNTY_LIVE", "true")
    monkeypatch.delenv("TASKBOUNTY_API_KEY", raising=False)
    assert deliver_result_to_taskbounty({"platform": "taskbounty", "bounty_id": "tb-1"}, "x", "job-3") is False
