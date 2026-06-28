"""Tests for the RentAHuman go-live preflight (no network)."""

from sovereign_os.oversight.rentahuman_preflight import run_preflight


def _ok_get(url, params, headers, timeout):
    if url.endswith("/escrow/agent-rentals"):
        return []
    raise AssertionError(f"unexpected url {url}")


def _status(report, name):
    return next(c["status"] for c in report["checks"] if c["name"] == name)


def test_dry_run_go_without_key():
    r = run_preflight(api_key="", live=False, get_json=_ok_get)
    assert r["go"] is True
    assert _status(r, "api_key") == "warn"
    assert _status(r, "post_fund_release_path") == "ok"


def test_live_without_key_is_no_go():
    r = run_preflight(api_key="", live=True, get_json=_ok_get)
    assert r["go"] is False
    assert _status(r, "api_key") == "blocker"


def test_live_with_key_reachable_is_go():
    r = run_preflight(api_key="rah_live_x", live=True, get_json=_ok_get)
    assert r["go"] is True
    assert _status(r, "account_reachable") == "ok"


def test_live_unreachable_account_blocks():
    def _boom(*a):
        raise RuntimeError("401 unauthorized")
    r = run_preflight(api_key="rah_live_x", live=True, get_json=_boom)
    assert r["go"] is False
    assert _status(r, "account_reachable") == "blocker"
