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


# ----------------------------------------------------- end-to-end loop
@pytest.mark.asyncio
async def test_clawtasks_loop_end_to_end():
    """discover -> job payload -> govern (plan/CFO/workers/audit) -> dry-run deliver."""
    from sovereign_os.agents.auth import SovereignAuth
    from sovereign_os.auditor import ReviewEngine
    from sovereign_os.auditor.review_engine import StubAuditor
    from sovereign_os.governance.engine import GovernanceEngine
    from sovereign_os.ingest_bridge.normalizer import to_job_payload
    from sovereign_os.ledger.unified_ledger import UnifiedLedger
    from sovereign_os.models.charter import Charter, CoreCompetency, FiscalBoundaries

    bounty = {"id": "e2e-1", "title": "Summarize AI agent market", "description": "3 bullets",
              "amount": 20, "currency": "USDC", "status": "open", "mode": "instant", "funded": True}
    source = ClawTasksOrderSource(get_json=lambda *a, **k: [bounty])
    orders = list(source.fetch())
    assert len(orders) == 1
    payload = to_job_payload(orders[0])

    charter = Charter(
        mission="Deliver content.",
        core_competencies=[CoreCompetency(name="research", description="research", priority=8)],
        fiscal_boundaries=FiscalBoundaries(daily_burn_max_usd=50.0, min_job_margin_ratio=0.2),
    )
    ledger = UnifiedLedger()
    ledger.record_usd(1000)
    engine = GovernanceEngine(charter, ledger, auth=SovereignAuth(),
                              review_engine=ReviewEngine(charter, judge=StubAuditor()))
    plan, results, reports = await engine.run_mission_with_audit(
        payload["goal"], abort_on_audit_failure=False, job_revenue_cents=payload["amount_cents"])
    assert results and all(r.passed for r in reports)

    client = ClawTasksClient("", live=False)
    deliverable = "\n".join(r.output for r in results)
    claim = client.claim(orders[0].contact["bounty_id"])
    sub = client.submit(orders[0].contact["bounty_id"], deliverable)
    assert claim["dry_run"] and sub["dry_run"]  # full loop ran, no funds moved


# ------------------------------------------- generic field-mapped bounty source
def test_generic_source_with_taskbounty_field_map():
    from sovereign_os.ingest_bridge.sources.bounty_board import taskbounty_source

    # Real TaskBounty schema (validated against live /api/v1/tasks):
    # id, title, short_summary, bounty_cents (already cents), status OPEN/AWARDED/CLOSED.
    rows = {"data": [
        {"id": "tb-1", "title": "Fix a bug", "short_summary": "in parser",
         "bounty_cents": 30000, "status": "OPEN", "currency": "usd", "tags": "[]"},
        {"id": "tb-2", "title": "Already awarded", "short_summary": "x",
         "bounty_cents": 10000, "status": "AWARDED"},
    ]}
    src = taskbounty_source(api_key="tb_live_x", get_json=lambda *a, **k: rows)
    orders = list(src.fetch())
    assert len(orders) == 1                       # AWARDED dropped, only OPEN kept
    o = orders[0]
    assert o.source_id == "taskbounty:tb-1"
    assert o.amount_cents == 30000                # bounty_cents already in cents (no x100)
    assert o.currency == "USD"
    assert "Fix a bug" in o.goal and "in parser" in o.goal
    assert o.contact["platform"] == "taskbounty"


def test_taskbounty_amount_band_uses_usd():
    from sovereign_os.ingest_bridge.sources.bounty_board import taskbounty_source

    rows = {"data": [{"id": "x", "title": "T", "short_summary": "S",
                      "bounty_cents": 30000, "status": "OPEN"}]}  # $300
    # Floor of $500 should drop a $300 bounty (band compared in USD, not cents).
    src = taskbounty_source(min_amount_usd=500, get_json=lambda *a, **k: rows)
    assert list(src.fetch()) == []
    src2 = taskbounty_source(min_amount_usd=100, get_json=lambda *a, **k: rows)
    assert len(list(src2.fetch())) == 1


def test_runner_registers_taskbounty_source():
    from sovereign_os.ingest_bridge.config import BridgeConfig, TaskBountySourceConfig
    from sovereign_os.ingest_bridge.runner import _sources_from_config
    from sovereign_os.ingest_bridge.sources.bounty_board import GenericBountySource

    cfg = BridgeConfig()
    cfg.taskbounty = TaskBountySourceConfig(enabled=True)
    sources = _sources_from_config(cfg)
    assert any(isinstance(s, GenericBountySource) and s.platform == "taskbounty" for s in sources)


def test_generic_source_wrapped_response_and_auth_header():
    from sovereign_os.ingest_bridge.sources.bounty_board import BountyFieldMap, GenericBountySource

    seen = {}

    def _get(url, params, headers, timeout):
        seen["headers"] = headers
        return {"data": [{"id": "g1", "title": "T", "description": "D", "amount": 5,
                          "status": "open", "funded": True}]}

    src = GenericBountySource(
        "https://x.test/api", field_map=BountyFieldMap(list_key="data"),
        headers={"Authorization": "Bearer k"}, get_json=_get,
    )
    orders = list(src.fetch())
    assert len(orders) == 1 and orders[0].source_id == "generic:g1"
    assert seen["headers"]["Authorization"] == "Bearer k"


# ------------------------------------------------------ StacksTasker source
def test_stackstasker_field_map():
    from sovereign_os.ingest_bridge.sources.bounty_board import stackstasker_source

    rows = {"tasks": [
        {"id": "st-1", "title": "Coding task", "description": "do X", "category": "coding",
         "bounty": "5", "status": "open", "network": "testnet"},
        {"id": "st-2", "title": "Done one", "description": "y", "bounty": "5", "status": "completed"},
    ]}
    src = stackstasker_source(get_json=lambda *a, **k: rows)
    orders = list(src.fetch())
    assert len(orders) == 1                       # completed dropped, only open
    o = orders[0]
    assert o.source_id == "stackstasker:st-1"
    assert o.amount_cents == 500                  # 5 STX -> 500 nominal units
    assert o.currency == "STX"


def test_runner_registers_stackstasker_source():
    from sovereign_os.ingest_bridge.config import BridgeConfig, StacksTaskerSourceConfig
    from sovereign_os.ingest_bridge.runner import _sources_from_config
    from sovereign_os.ingest_bridge.sources.bounty_board import GenericBountySource

    cfg = BridgeConfig()
    cfg.stackstasker = StacksTaskerSourceConfig(enabled=True)
    sources = _sources_from_config(cfg)
    assert any(isinstance(s, GenericBountySource) and s.platform == "stackstasker" for s in sources)


# --------------------------------------------------------- BotBounty source
def test_botbounty_field_map_and_per_record_currency():
    from sovereign_os.ingest_bridge.sources.bounty_board import botbounty_source

    rows = {"count": 2, "bounties": [
        {"id": "bb-1", "title": "CSV to JSON", "description": "convert", "category": "code",
         "amount": 25, "currency": "USDC", "status": "open"},
        {"id": "bb-2", "title": "Closed one", "description": "x", "amount": 10,
         "currency": "ETH", "status": "completed"},
    ]}
    src = botbounty_source(get_json=lambda *a, **k: rows)
    orders = list(src.fetch())
    assert len(orders) == 1                       # only open kept
    o = orders[0]
    assert o.source_id == "botbounty:bb-1"
    assert o.amount_cents == 2500                 # amount 25 -> cents
    assert o.currency == "USDC"                   # per-record currency, not the fallback


def test_runner_registers_botbounty_source():
    from sovereign_os.ingest_bridge.config import BridgeConfig, BotBountySourceConfig
    from sovereign_os.ingest_bridge.runner import _sources_from_config
    from sovereign_os.ingest_bridge.sources.bounty_board import GenericBountySource

    cfg = BridgeConfig()
    cfg.botbounty = BotBountySourceConfig(enabled=True)
    sources = _sources_from_config(cfg)
    assert any(isinstance(s, GenericBountySource) and s.platform == "botbounty" for s in sources)
