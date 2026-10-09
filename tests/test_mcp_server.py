"""
Tests for the MCP server surface and for governance on the MCP client path.

Two halves of the same gap. The client path let any registered tool be invoked by any
task with no authority check at all — an MCP tool reaches the filesystem, the network, or
someone's API, so "a tool is registered" was standing in for "this task may use it".

The server is the product's on-ramp, and the thing worth pinning there is what it does
NOT expose: no tool raises a budget, grants a capability, or disables a check. A
governance layer whose controls are reachable from the agent it governs is decorative —
the first thing a model does on hitting a ceiling is look for the lever.
"""

import json

import pytest

from sovereign_os.agents.auth import Capability, SovereignAuth
from sovereign_os.agents.delegation import DelegationBroker
from sovereign_os.agents.delegation_gate import (
    active_grant,
    authorize_tool_call,
    set_broker,
    task_authority,
)
from sovereign_os.mcp import server as mcp_server

API = Capability.CALL_EXTERNAL_API
WRITE = Capability.WRITE_FILES


@pytest.fixture(autouse=True)
def clean_broker():
    set_broker(None)
    yield
    set_broker(None)


@pytest.fixture
def broker():
    auth = SovereignAuth()
    auth._set_score("worker-1", 95)
    b = DelegationBroker(eligibility=auth.check_permission_for)
    set_broker(b)
    return b


# ------------------------------------------------- governing the client path

def test_a_tool_call_needs_the_authority_to_leave_the_process(broker):
    """CALL_EXTERNAL_API is the floor: an MCP tool call leaves the process by definition."""
    grant = broker.root("worker-1", task_id="t1", capabilities={Capability.READ_FILES},
                        budget_cents=100)
    with task_authority(grant.grant_id):
        permitted, reason = authorize_tool_call(tool_name="fetch", server_id="web")
    assert permitted is False and "call_external_api" in reason


def test_a_grant_carrying_it_is_authorized(broker):
    grant = broker.root("worker-1", task_id="t1", capabilities={API}, budget_cents=100)
    with task_authority(grant.grant_id):
        permitted, _ = authorize_tool_call(tool_name="fetch", server_id="web")
    assert permitted is True


def test_a_server_can_declare_more_than_the_floor(broker):
    """
    A filesystem server writes. Declared per server rather than guessed from a tool's
    name — a name-based classifier standing between an agent and the filesystem is a
    guess wearing a policy's clothes.
    """
    grant = broker.root("worker-1", task_id="t1", capabilities={API}, budget_cents=100)
    with task_authority(grant.grant_id):
        permitted, reason = authorize_tool_call(
            tool_name="write_file", server_id="filesystem", capabilities={API, WRITE})
    assert permitted is False and "write_files" in reason


def test_authority_does_not_leak_past_the_task(broker):
    grant = broker.root("worker-1", task_id="t1", capabilities={API}, budget_cents=100)
    with task_authority(grant.grant_id):
        assert active_grant() == grant.grant_id
    assert active_grant() == ""
    permitted, reason = authorize_tool_call(tool_name="fetch")
    assert "no active grant" in reason or permitted is True   # permissive default


def test_strict_mode_refuses_an_ungoverned_tool_call(broker, monkeypatch):
    monkeypatch.setenv("SOVEREIGN_STRICT_DELEGATION", "1")
    permitted, reason = authorize_tool_call(tool_name="fetch", server_id="web")
    assert permitted is False and "no delegation grant is active" in reason


def test_without_governance_the_historical_behaviour_is_preserved():
    permitted, reason = authorize_tool_call(tool_name="fetch")
    assert permitted is True and "no delegation governance" in reason


def test_a_revoked_task_cannot_keep_calling_tools(broker):
    grant = broker.root("worker-1", task_id="t1", capabilities={API}, budget_cents=100)
    broker.revoke_task("t1")
    with task_authority(grant.grant_id):
        permitted, _ = authorize_tool_call(tool_name="fetch", server_id="web")
    assert permitted is False


def test_the_declared_capability_env_is_parsed(monkeypatch):
    from sovereign_os.mcp.live import _required_capabilities

    monkeypatch.setenv("SOVEREIGN_MCP_CAPABILITIES",
                       json.dumps({"filesystem": ["write_files"]}))
    assert _required_capabilities("filesystem") == frozenset({API, WRITE})
    assert _required_capabilities("web") == frozenset({API})


def test_a_malformed_declaration_does_not_widen_authority(monkeypatch):
    """Failing open on a parse error would be the worst possible direction."""
    from sovereign_os.mcp.live import _required_capabilities

    monkeypatch.setenv("SOVEREIGN_MCP_CAPABILITIES", "{not json")
    assert _required_capabilities("filesystem") == frozenset({API})


# --------------------------------------------------------- the server surface

def test_the_exposed_tools_are_the_intended_four():
    assert set(mcp_server.TOOLS) == {
        "forecast_cost", "run_governed", "audit_receipt", "governance_status"}


def test_no_tool_can_loosen_governance():
    """
    The load-bearing property. A model that hits a ceiling will look for the lever, so
    there must not be one on this surface.
    """
    forbidden = ("budget", "grant", "capab", "limit", "disable", "override", "approve",
                 "trust", "charter", "revoke", "allow")
    for name, spec in mcp_server.TOOLS.items():
        if name == "governance_status":
            continue                      # read-only reporting
        assert not any(word in name.lower() for word in forbidden), name
    assert "set_budget" not in mcp_server.TOOLS
    assert "grant_capability" not in mcp_server.TOOLS


def test_every_tool_has_a_schema_and_description():
    for name, spec in mcp_server.TOOLS.items():
        assert spec["description"].strip(), name
        assert spec["schema"]["type"] == "object", name


@pytest.mark.asyncio
async def test_forecast_is_honest_about_having_no_evidence():
    from sovereign_os.governance.cost_model import reset_cost

    reset_cost()
    d = await mcp_server.dispatch("forecast_cost", {"goal": "Write a function.",
                                                    "category": "coding"})
    assert d["estimate_cents"] > 0
    assert d["upper_cents_p80"] is None, "a band from no samples is invented precision"
    assert "uncalibrated" in d["note"]


@pytest.mark.asyncio
async def test_forecast_reports_a_band_once_there_is_a_spread():
    from sovereign_os.governance.cost_model import record_cost, reset_cost

    reset_cost()
    for actual in (40, 90, 150, 220, 60, 300):
        record_cost("coding", 100, actual)
    d = await mcp_server.dispatch("forecast_cost", {"goal": "Write a function.",
                                                    "category": "coding"})
    assert d["calibrated_on_samples"] == 6
    assert d["upper_cents_p80"] is not None
    reset_cost()


@pytest.mark.asyncio
async def test_an_unknown_tool_is_rejected_by_name():
    with pytest.raises(KeyError):
        await mcp_server.dispatch("set_budget", {"cents": 999999})


@pytest.mark.asyncio
async def test_audit_receipt_explains_how_to_verify(tmp_path, monkeypatch):
    monkeypatch.setenv("SOVEREIGN_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SOVEREIGN_AUDIT_TRAIL_PATH", str(tmp_path / "audit.jsonl"))
    (tmp_path / "audit.jsonl").write_text(
        json.dumps({"task_id": "t1", "passed": True, "proof_hash": "abc123"}) + "\n",
        encoding="utf-8")

    d = await mcp_server.dispatch("audit_receipt", {"limit": 5})

    assert d["entries"][0]["proof_hash"] == "abc123"
    assert "recompute" in d["verify"], "a receipt nobody can check is a summary"


@pytest.mark.asyncio
async def test_audit_receipt_is_empty_rather_than_failing(tmp_path, monkeypatch):
    monkeypatch.setenv("SOVEREIGN_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SOVEREIGN_AUDIT_TRAIL_PATH", str(tmp_path / "nothing.jsonl"))
    d = await mcp_server.dispatch("audit_receipt", {})
    assert d["entries"] == [] and "run something first" in d["note"]
