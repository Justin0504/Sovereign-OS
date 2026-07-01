"""Tests for StacksTasker bid+submit delivery-back (dry-run default + injected http)."""

from sovereign_os.delivery.stackstasker import deliver_result_to_stackstasker


def test_no_bounty_id():
    assert deliver_result_to_stackstasker({"platform": "stackstasker"}, "x", "job-1") is False


def test_dry_run_by_default(monkeypatch):
    monkeypatch.delenv("STACKSTASKER_LIVE", raising=False)
    assert deliver_result_to_stackstasker({"platform": "stackstasker", "bounty_id": "st-1"}, "done", "job-1") is True


def test_live_bids_then_submits(monkeypatch):
    monkeypatch.setenv("STACKSTASKER_LIVE", "true")
    monkeypatch.setenv("STACKSTASKER_AGENT_ID", "agentX")
    calls = []
    def _post(url, body, headers, timeout):
        calls.append((url, body)); return {"ok": True}
    ok = deliver_result_to_stackstasker({"platform": "stackstasker", "bounty_id": "st-9"},
                                        "solution text", "job-2", post_json=_post)
    assert ok is True and len(calls) == 2
    assert "/tasks/st-9/bid" in calls[0][0] and calls[0][1]["agentId"] == "agentX"
    assert "/tasks/st-9/submit" in calls[1][0] and calls[1][1]["result"] == "solution text"


def test_live_without_agent_id_skips(monkeypatch):
    monkeypatch.setenv("STACKSTASKER_LIVE", "true")
    monkeypatch.delenv("STACKSTASKER_AGENT_ID", raising=False)
    assert deliver_result_to_stackstasker({"platform": "stackstasker", "bounty_id": "st-1"}, "x", "job-3") is False
