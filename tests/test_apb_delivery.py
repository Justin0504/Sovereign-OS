"""Tests for the APB (Agent Payment Bounty) delivery/claim adapter."""

from sovereign_os.delivery.apb import _claim_url, deliver_result_to_apb


def test_claim_url_extraction():
    assert _claim_url("https://pub/claim/1") == "https://pub/claim/1"
    assert _claim_url("http://pub/s") == "http://pub/s"
    assert _claim_url({"submitUrl": "https://pub/s"}) == "https://pub/s"
    assert _claim_url({"endpoint": "https://pub/e"}) == "https://pub/e"
    assert _claim_url("just do these steps") == ""
    assert _claim_url({"note": "no url here"}) == ""
    assert _claim_url(None) == ""


def test_no_bounty_id_returns_false():
    assert deliver_result_to_apb({}, "result", "job-1") is False


def test_dry_run_by_default(monkeypatch):
    monkeypatch.delenv("APB_LIVE", raising=False)
    called = {"n": 0}

    def poster(*a, **k):
        called["n"] += 1
        return {}

    ok = deliver_result_to_apb(
        {"bounty_id": "b1", "claim": "https://pub/claim/b1", "pay_to": "0xabc"},
        "the deliverable", "job-1", post_json=poster,
    )
    assert ok is True and called["n"] == 0  # dry-run posts nothing


def test_live_posts_to_claim_url_with_payload(monkeypatch):
    monkeypatch.setenv("APB_LIVE", "true")
    monkeypatch.setenv("APB_API_KEY", "tok123")
    cap = {}

    def poster(url, body, headers, timeout):
        cap.update(url=url, body=body, headers=headers)
        return {"ok": True}

    ok = deliver_result_to_apb(
        {"bounty_id": "b1", "claim": "https://pub/claim/b1", "pay_to": "0xabc",
         "network": "base", "asset": "USDC"},
        "the result", "job-1", post_json=poster,
    )
    assert ok is True
    assert cap["url"] == "https://pub/claim/b1"
    assert cap["body"]["bountyId"] == "b1"
    assert cap["body"]["result"] == "the result"
    assert cap["body"]["payTo"] == "0xabc"
    assert cap["headers"]["Authorization"] == "Bearer tok123"


def test_live_without_claim_url_returns_false(monkeypatch):
    monkeypatch.setenv("APB_LIVE", "true")
    called = {"n": 0}

    def poster(*a, **k):
        called["n"] += 1
        return {}

    ok = deliver_result_to_apb({"bounty_id": "b2", "claim": "do X then Y"}, "r", "job-2", post_json=poster)
    assert ok is False and called["n"] == 0  # no URL -> nothing posted


def test_live_swallows_post_errors(monkeypatch):
    monkeypatch.setenv("APB_LIVE", "true")

    def boom(*a, **k):
        raise RuntimeError("publisher 500")

    ok = deliver_result_to_apb({"bounty_id": "b3", "claim": "https://pub/c"}, "r", "job-3", post_json=boom)
    assert ok is False  # never raises
