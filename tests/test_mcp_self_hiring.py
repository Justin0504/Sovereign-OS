"""Tests for MCP tool graph and MCPWorker (Phase 5 self-hiring)."""

import pytest

from sovereign_os.agents.mcp_worker import MCPWorker
from sovereign_os.agents.base import TaskInput
from sovereign_os.mcp.client import MCPToolSchema
from sovereign_os.mcp.tool_graph import MCPToolGraph


@pytest.fixture
def sample_tools():
    return [
        ("s1", MCPToolSchema(name="search", description="Search", input_schema={})),
        ("s1", MCPToolSchema(name="read_file", description="Read file", input_schema={})),
    ]


def test_mcp_tool_graph_get_tools_for_skill_empty():
    graph = MCPToolGraph()
    assert graph.get_tools_for_skill("research") == []
    assert graph.has_tools_for_skill("research") is False
    assert graph.discover_skills() == set()


@pytest.mark.asyncio
async def test_mcp_tool_graph_add_server_and_resolve():
    graph = MCPToolGraph()

    class FakeClient:
        async def list_tools(self):
            return [
                MCPToolSchema(name="search", description="Search"),
                MCPToolSchema(name="read_file", description="Read"),
            ]

    client = FakeClient()
    await graph.add_server("mock", client)
    assert graph.has_tools_for_skill("research") is True
    tools = graph.get_tools_for_skill("research")
    assert len(tools) >= 1
    assert tools[0][0] == "mock"
    assert graph.get_client("mock") is client
    skills = graph.discover_skills()
    assert "research" in skills


@pytest.mark.asyncio
async def test_mcp_worker_no_tools_returns_failure():
    worker = MCPWorker(
        agent_id="mcp-test",
        system_prompt="Test",
        skill="research",
        tools=[],
        get_client=lambda s: None,
    )
    result = await worker.execute(
        TaskInput(task_id="t1", description="Find X", required_skill="research")
    )
    assert result.success is False
    assert "No tools" in result.output


@pytest.mark.asyncio
async def test_mcp_worker_calls_tool_and_returns_result(sample_tools):
    call_log = []

    class FakeClient:
        async def call_tool(self, name, arguments):
            call_log.append((name, arguments))
            return {"content": [{"text": "Result for " + name}], "isError": False}

    def get_client(server_id):
        return FakeClient()

    worker = MCPWorker(
        agent_id="mcp-research",
        system_prompt="Test",
        skill="research",
        tools=sample_tools,
        get_client=get_client,
    )
    result = await worker.execute(
        TaskInput(task_id="t1", description="Search for market data", required_skill="research")
    )
    assert result.success is True
    assert "Result for search" in result.output or "Result for read_file" in result.output
    assert len(call_log) == 1
    assert call_log[0][0] in ("search", "read_file")
