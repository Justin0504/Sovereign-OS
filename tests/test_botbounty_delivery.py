"""Tests for the BotBounty delivery adapter (the previously-missing last mile) and
the ingest field-drift guard / claim-endpoint passthrough."""

import logging

from sovereign_os.delivery.botbounty import deliver_result_to_botbounty
from sovereign_os.ingest_bridge.sources.bounty_board import botbounty_source


def test_no_bounty_id_returns_false():
    assert deliver_result_to_botbounty({"platform": "botbounty"}, "x", "job-1") is False


def test_dry_run_by_default(monkeypatch):
    monkeypatch.delenv("BOTBOUNTY_LIVE", raising=False)
    calls = {"n": 0}

    def _post(*a, **k):
        calls["n"] += 1
        return {}

    ok = deliver_result_to_botbounty({"platform": "botbounty", "bounty_id": "b1"},
                                     "solution", "job-1", post_json=_post)
    assert ok is True and calls["n"] == 0  # dry-run posts nothing


def test_live_claims_then_submits(monkeypatch):
    monkeypatch.setenv("BOTBOUNTY_LIVE", "true")
    monkeypatch.setenv("BOTBOUNTY_API_KEY", "bb_live_x")
    monkeypatch.setenv("BOTBOUNTY_AGENT_ID", "agent-7")
    calls = []

    def _post(url, body, headers, timeout):
        calls.append((url, body, headers.get("Authorization")))
        return {"ok": True}

    ok = deliver_result_to_botbounty(
        {"platform": "botbounty", "bounty_id": "b9",
         "claim_endpoint": "https://bb/api/agent/bounties/b9/claim"},
        "the solution", "job-2", post_json=_post,
    )
    assert ok is True and len(calls) == 2
    assert calls[0][0] == "https://bb/api/agent/bounties/b9/claim"     # uses provided claim endpoint
    assert calls[0][2] == "Bearer bb_live_x"
    assert calls[1][1]["solution"] == "the solution" and calls[1][1]["agentId"] == "agent-7"


def test_live_without_api_key_skips(monkeypatch):
    monkeypatch.setenv("BOTBOUNTY_LIVE", "true")
    monkeypatch.delenv("BOTBOUNTY_API_KEY", raising=False)
    assert deliver_result_to_botbounty({"platform": "botbounty", "bounty_id": "b1"}, "x", "job-3") is False


def test_live_constructs_claim_url_from_base(monkeypatch):
    monkeypatch.setenv("BOTBOUNTY_LIVE", "true")
    monkeypatch.setenv("BOTBOUNTY_API_KEY", "k")
    monkeypatch.setenv("BOTBOUNTY_API_BASE", "https://bb.example/api")
    calls = []

    def _post(url, body, headers, timeout):
        calls.append(url)
        return {}

    deliver_result_to_botbounty({"platform": "botbounty", "bounty_id": "b5"}, "s", "job-5", post_json=_post)
    assert calls[0] == "https://bb.example/api/agent/bounties/b5/claim"


def test_live_swallows_errors(monkeypatch):
    monkeypatch.setenv("BOTBOUNTY_LIVE", "true")
    monkeypatch.setenv("BOTBOUNTY_API_KEY", "k")

    def _boom(*a, **k):
        raise RuntimeError("500")

    assert deliver_result_to_botbounty({"platform": "botbounty", "bounty_id": "b1"}, "x", "job-6", post_json=_boom) is False


# ------------------------------------------------ ingest guard + passthrough
def _rows(rows):
    def get(url, params, headers, timeout):
        return {"bounties": rows}
    return get


def test_ingest_captures_claim_endpoint():
    src = botbounty_source(get_json=_rows([
        {"id": "b1", "title": "Fix bug", "amount": 5, "status": "open",
         "claimEndpoint": "https://bb/api/agent/bounties/b1/claim"},
    ]))
    orders = list(src.fetch())
    assert orders[0].contact["claim_endpoint"] == "https://bb/api/agent/bounties/b1/claim"


def test_ingest_warns_on_field_drift(caplog):
    src = botbounty_source(get_json=_rows([
        {"id": "b2", "title": "Task", "reward_value": 500, "status": "open"},  # 'amount' renamed -> drift
    ]))
    with caplog.at_level(logging.WARNING):
        orders = list(src.fetch())
    assert orders[0].amount_cents == 0
    assert any("no 'amount' field" in rec.message for rec in caplog.records)
