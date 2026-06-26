"""Tests for the ClawTasks go-live preflight safety check (no network)."""

from sovereign_os.ingest_bridge.clawtasks_preflight import run_preflight


def _cfg(free=False):
    def _get(url, params, headers, timeout):
        if url.endswith("/config"):
            return {"chain_id": 8453, "stake_percent": 10, "min_bounty": 1, "free_tasks_only": free}
        if url.endswith("/agents/me/pending"):
            return []
        raise AssertionError(f"unexpected url {url}")
    return _get


def _status(report, name):
    return next(c["status"] for c in report["checks"] if c["name"] == name)


def test_dry_run_is_go_without_key():
    r = run_preflight(api_key="", live=False, get_json=_cfg())
    assert r["go"] is True            # no blockers in dry-run
    assert r["live"] is False
    assert _status(r, "api_key") == "warn"     # missing key only warns in dry-run
    assert _status(r, "claim_submit_path") == "ok"


def test_live_without_key_is_no_go():
    r = run_preflight(api_key="", live=True, get_json=_cfg())
    assert r["go"] is False           # missing key is a blocker when live
    assert _status(r, "api_key") == "blocker"
    assert _status(r, "mode") == "warn"


def test_live_with_key_is_go():
    r = run_preflight(api_key="tok", live=True, get_json=_cfg())
    assert r["go"] is True
    assert _status(r, "api_key") == "ok"
    assert _status(r, "pending_reachable") == "ok"


def test_free_tasks_mode_warns_not_blocks():
    r = run_preflight(api_key="tok", live=True, get_json=_cfg(free=True))
    assert r["go"] is True            # warning, not a blocker
    assert _status(r, "free_tasks_only") == "warn"


def test_unreachable_config_blocks_when_live():
    def _boom(*a):
        raise RuntimeError("500 server error")
    r = run_preflight(api_key="tok", live=True, get_json=_boom)
    assert r["go"] is False
    assert _status(r, "config_reachable") == "blocker"
