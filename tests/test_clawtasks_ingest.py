"""
Tests for the ClawTasks integration: discovery source mapping/filtering,
claim/submit dry-run gating, config + runner wiring, and delivery.
All HTTP is injected — no network calls.
"""

import pytest

from sovereign_os.ingest_bridge.sources.base import RawOrder
from sovereign_os.ingest_bridge.sources.clawtasks import (
    ClawTasksClient,
    ClawTasksOrderSource,
)


def _bounties():
    return [
        {"id": "b1", "title": "Write a blog post", "description": "About AI agents",
         "amount": 25, "currency": "USDC", "status": "open", "mode": "instant",
         "funded": True, "deadline_hours": 24, "tags": ["writing"], "poster": "alice"},
        {"id": "b2", "title": "Unfunded task", "description": "x", "amount": 50,
         "status": "open", "mode": "instant", "funded": False},                 # dropped: unfunded
        {"id": "b3", "title": "Assigned task", "description": "x", "amount": 30,
         "status": "open", "funded": True, "assigned_to": "someone-else"},        # dropped: direct-hire
        {"id": "b4", "title": "Closed", "description": "x", "amount": 30,
         "status": "claimed", "funded": True},                                    # dropped: not open
    ]


def _fake_get(bounties):
    def _get(url, params, headers, timeout):
        assert "/bounties" in url
        assert params.get("status") == "open"
        return bounties
    return _get


# ----------------------------------------------------------------- source
def test_source_maps_open_funded_bounties():
    src = ClawTasksOrderSource(get_json=_fake_get(_bounties()))
    orders = list(src.fetch())
    assert len(orders) == 1                         # only b1 passes all filters
    o = orders[0]
    assert isinstance(o, RawOrder)
    assert o.source_id == "clawtasks:b1"
    assert o.amount_cents == 2500                   # $25 USDC
    assert o.currency == "USDC"
    assert "Write a blog post" in o.goal and "About AI agents" in o.goal
    assert o.contact["platform"] == "clawtasks"
    assert o.contact["bounty_id"] == "b1"
    assert o.meta["poster"] == "alice"


def test_source_amount_band_filter():
    src = ClawTasksOrderSource(min_amount_usd=30, get_json=_fake_get(_bounties()))
    # b1 ($25) now below the floor -> nothing passes.
    assert list(src.fetch()) == []


def test_source_handles_wrapped_response():
    src = ClawTasksOrderSource(get_json=lambda *a: {"bounties": _bounties()})
    assert len(list(src.fetch())) == 1


def test_source_survives_fetch_error():
    def _boom(*a):
        raise RuntimeError("network down")
    src = ClawTasksOrderSource(get_json=_boom)
    assert list(src.fetch()) == []  # logged, no raise


# ----------------------------------------------------------------- client
def test_client_dry_run_does_not_post():
    posted = []
    client = ClawTasksClient("key", live=False, post_json=lambda *a: posted.append(a))
    claim = client.claim("b1")
    sub = client.submit("b1", "result")
    assert claim["dry_run"] is True and sub["dry_run"] is True
    assert posted == []  # no network in dry-run


def test_client_live_posts():
    calls = []

    def _post(url, body, headers, timeout):
        calls.append((url, body, headers))
        return {"ok": True}

    client = ClawTasksClient("key", live=True, post_json=_post)
    client.claim("b1")
    client.submit("b1", "x" * 60_000)  # truncated to 50k
    assert len(calls) == 2
    assert calls[0][0].endswith("/bounties/b1/claim")
    assert calls[0][2]["Authorization"] == "Bearer key"
    assert calls[1][0].endswith("/bounties/b1/submit")
    assert len(calls[1][1]["content"]) == 50_000


def test_client_live_requires_api_key():
    client = ClawTasksClient("", live=True)
    with pytest.raises(ValueError):
        client.claim("b1")


# --------------------------------------------------------- config + runner
def test_config_from_env(monkeypatch):
    from sovereign_os.ingest_bridge.config import BridgeConfig

    monkeypatch.setenv("BRIDGE_CLAWTASKS_ENABLED", "true")
    monkeypatch.setenv("CLAWTASKS_MIN_AMOUNT_USD", "10")
    monkeypatch.setenv("CLAWTASKS_TAGS", "writing, research")
    cfg = BridgeConfig.from_env()
    assert cfg.clawtasks.enabled is True
    assert cfg.clawtasks.min_amount_usd == 10.0
    assert cfg.clawtasks.tags == ["writing", "research"]


def test_runner_registers_clawtasks_source():
    from sovereign_os.ingest_bridge.config import BridgeConfig, ClawTasksSourceConfig
    from sovereign_os.ingest_bridge.runner import _sources_from_config

    cfg = BridgeConfig()
    cfg.clawtasks = ClawTasksSourceConfig(enabled=True)
    sources = _sources_from_config(cfg)
    assert any(isinstance(s, ClawTasksOrderSource) for s in sources)


# ---------------------------------------------------------------- delivery
def test_delivery_dry_run(monkeypatch):
    from sovereign_os.delivery.clawtasks import deliver_result_to_clawtasks

    monkeypatch.delenv("CLAWTASKS_LIVE", raising=False)
    ok = deliver_result_to_clawtasks({"platform": "clawtasks", "bounty_id": "b1"}, "the deliverable", "job-1")
    assert ok is True  # dry-run flow ran, no funds moved


def test_delivery_no_bounty_id():
    from sovereign_os.delivery.clawtasks import deliver_result_to_clawtasks

    assert deliver_result_to_clawtasks({"platform": "clawtasks"}, "x", "job-1") is False
