"""Tests for the live MCP bridge: any registered MCP server's tools in worker loops."""

import json

import pytest

from sovereign_os.agents.base import BaseWorker
from sovereign_os.mcp import live
from sovereign_os.mcp.client import MCPToolSchema


class _Concrete(BaseWorker):
    async def execute(self, task):  # pragma: no cover
        ...


class _ScriptLLM:
    model_name = "fake"

    def __init__(self, replies):
        self.replies = list(replies)
        self.i = 0
        self._last_usage = {"input_tokens": 1, "output_tokens": 1}

    async def chat(self, messages):
        r = self.replies[min(self.i, len(self.replies) - 1)]
        self.i += 1
        return r


class _FakeMCPClient:
    def __init__(self, tools=None, result=None, raise_list=False):
        self._tools = tools or [MCPToolSchema(name="db_query", description="Query the DB")]
        self._result = result or {"content": [{"type": "text", "text": "ok"}], "isError": False}
        self._raise_list = raise_list
        self.calls = []

    async def list_tools(self):
        if self._raise_list:
            raise RuntimeError("server down")
        return self._tools

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return self._result


@pytest.fixture(autouse=True)
def _clean_registry():
    live.clear()
    yield
    live.clear()


def _final(o):
    return json.dumps({"action": "final", "output": o})


def _tool(name, **args):
    return json.dumps({"action": "tool", "tool": name, "args": args})


# ------------------------------------------------------------------- registry
def test_register_and_clear():
    assert live.has_servers() is False
    live.register_client("s1", _FakeMCPClient())
    assert live.has_servers() and live.registered_server_ids() == ["s1"]
    live.unregister_client("s1")
    assert live.has_servers() is False


def test_content_to_text_variants():
    assert live._content_to_text({"content": [{"type": "text", "text": "hi"}]}) == "hi"
    assert live._content_to_text({"content": [{"text": "a"}, {"text": "b"}]}) == "a\nb"
    assert live._content_to_text({"content": [{"text": "boom"}], "isError": True}).startswith("(tool error)")


@pytest.mark.asyncio
async def test_mcp_tool_handlers_lists_and_caches():
    client = _FakeMCPClient(tools=[MCPToolSchema(name="search", description="web search")])
    live.register_client("s1", client)
    handlers, descs = await live.mcp_tool_handlers()
    assert "search" in handlers and descs["search"] == "web search"
    obs = await handlers["search"]({"q": "x"})
    assert obs == "ok" and client.calls == [("search", {"q": "x"})]


@pytest.mark.asyncio
async def test_bad_server_does_not_raise():
    live.register_client("bad", _FakeMCPClient(raise_list=True))
    handlers, _ = await live.mcp_tool_handlers()
    assert handlers == {}  # failed list_tools -> no tools, no exception


# ------------------------------------------------------------- worker tool loop
@pytest.mark.asyncio
async def test_no_servers_is_noop():
    w = _Concrete("w", "")
    w.llm = _ScriptLLM([_final("done")])
    out, _u, log = await w.run_with_tools("sys", "u", {}, max_steps=3)
    assert out == "done" and log == []


@pytest.mark.asyncio
async def test_worker_calls_registered_mcp_tool():
    live.register_client("analytics", _FakeMCPClient(
        tools=[MCPToolSchema(name="db_query", description="Query DB")],
        result={"content": [{"type": "text", "text": "42 rows"}]},
    ))
    w = _Concrete("w", "")
    w.llm = _ScriptLLM([_tool("db_query", sql="SELECT 1"), _final("done from db")])
    out, _u, log = await w.run_with_tools("sys", "analyze", {}, max_steps=4)
    assert out == "done from db"
    assert log[0]["tool"] == "db_query" and log[0]["obs"] == "42 rows"


@pytest.mark.asyncio
async def test_builtin_tool_wins_on_name_collision():
    live.register_client("s", _FakeMCPClient(
        tools=[MCPToolSchema(name="lookup")],
        result={"content": [{"text": "from mcp"}]},
    ))

    def builtin(args):
        return "from builtin"

    w = _Concrete("w", "")
    w.llm = _ScriptLLM([_tool("lookup"), _final("x")])
    _out, _u, log = await w.run_with_tools("sys", "u", {"lookup": builtin}, max_steps=3)
    assert log[0]["obs"] == "from builtin"  # built-in handler preserved


@pytest.mark.asyncio
async def test_connect_from_env_empty_and_malformed(monkeypatch):
    monkeypatch.delenv("SOVEREIGN_MCP_SERVERS", raising=False)
    assert await live.connect_from_env() == 0
    monkeypatch.setenv("SOVEREIGN_MCP_SERVERS", "{not json")
    assert await live.connect_from_env() == 0
    assert live.has_servers() is False


@pytest.mark.asyncio
async def test_connect_from_env_registers_server(monkeypatch):
    class _Stub:
        def __init__(self, **kw):
            self.kw = kw
        async def connect(self):
            return None
        async def list_tools(self):
            return [MCPToolSchema(name="t1", description="d")]

    monkeypatch.setattr("sovereign_os.mcp.live.MCPClient", _Stub)
    monkeypatch.setenv("SOVEREIGN_MCP_SERVERS",
                       '[{"id": "s1", "transport": "http", "url": "http://x/mcp"}]')
    n = await live.connect_from_env()
    assert n == 1 and live.registered_server_ids() == ["s1"]
    handlers, _ = await live.mcp_tool_handlers()
    assert "t1" in handlers


@pytest.mark.asyncio
async def test_verified_loop_also_gets_mcp_tools():
    live.register_client("s", _FakeMCPClient(
        tools=[MCPToolSchema(name="fetch")], result={"content": [{"text": "data"}]},
    ))
    w = _Concrete("w", "")
    w.llm = _ScriptLLM([_tool("fetch"), _final("done")])
    _out, _u, log, verified = await w.run_with_verified_tools(
        "sys", "u", {}, verifier=lambda: (True, "ok"), max_steps=4,
    )
    assert verified is True
    assert any(e["tool"] == "fetch" and e["obs"] == "data" for e in log)
