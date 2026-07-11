"""
Live MCP tool bridge: expose any connected MCP server's tools to ordinary workers.

Sovereign-OS already has an MCP *client* (`mcp/client.py`) and, when a skill has no
built-in worker, an `MCPWorker` that calls a single MCP tool. This module closes the
remaining gap — letting a normal worker's tool-use loop call **any** registered MCP
server's tools inline, alongside its built-in tools (web_fetch, code_workspace, ...).

An operator registers MCP servers once at startup; from then on their tools appear in
every worker's `run_with_tools` loop. Registering a server is the opt-in — with none
registered this is a no-op and worker behavior is unchanged.

MCP is the connector-compatibility layer: because Claude Agent SDK, Codex, and most
tools speak MCP, pointing Sovereign-OS at a new MCP server grants new connectors with
no code change.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Callable

from sovereign_os.mcp.client import MCPClient, MCPToolSchema

logger = logging.getLogger(__name__)

# Registered servers and their cached tool schemas (populated on registration/refresh).
_clients: dict[str, MCPClient] = {}
_tools: dict[str, list[MCPToolSchema]] = {}


def register_client(server_id: str, client: MCPClient, *, tools: list[MCPToolSchema] | None = None) -> None:
    """Register a connected MCP client. If `tools` is given they're cached now; else
    they're listed lazily on first use (see `_ensure_tools`)."""
    _clients[server_id] = client
    if tools is not None:
        _tools[server_id] = list(tools)
    logger.info("MCP live: registered server '%s' (%d tools cached)", server_id, len(tools or []))


def unregister_client(server_id: str) -> None:
    _clients.pop(server_id, None)
    _tools.pop(server_id, None)


def clear() -> None:
    """Drop all registered servers (used by tests)."""
    _clients.clear()
    _tools.clear()


def registered_server_ids() -> list[str]:
    return list(_clients)


def has_servers() -> bool:
    return bool(_clients)


async def _ensure_tools(server_id: str) -> list[MCPToolSchema]:
    if server_id in _tools:
        return _tools[server_id]
    client = _clients.get(server_id)
    if client is None:
        return []
    try:
        schemas = await client.list_tools()
    except Exception as e:  # noqa: BLE001 - a bad server must not break the worker
        logger.warning("MCP live: list_tools failed for '%s': %s", server_id, e)
        schemas = []
    _tools[server_id] = schemas
    return schemas


def _content_to_text(result: dict[str, Any]) -> str:
    """Flatten an MCP tools/call result (content array of {type,text}) to a string."""
    if not isinstance(result, dict):
        return str(result)
    prefix = "(tool error) " if result.get("isError") else ""
    content = result.get("content")
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text", item.get("data", ""))))
            else:
                parts.append(str(item))
        return prefix + "\n".join(p for p in parts if p)
    return prefix + str(result.get("result", result))


def _make_handler(client: MCPClient, tool_name: str) -> Callable[[dict], Any]:
    async def _handler(args: dict) -> str:
        try:
            res = await client.call_tool(tool_name, args or {})
        except Exception as e:  # noqa: BLE001 - surfaced to the model as an observation
            return f"(mcp tool error: {e})"
        return _content_to_text(res)[:4000]
    return _handler


async def connect_from_env() -> int:
    """
    Connect + register the MCP servers declared in `SOVEREIGN_MCP_SERVERS` (a JSON
    list). Each entry: {"id": "...", "transport": "stdio"|"http", "command": [...]}
    for stdio or {"...","transport":"http","url":"..."} for HTTP. Returns the count
    connected. Best-effort: a bad entry is logged and skipped, never fatal. Call once
    at startup (e.g. from the web/TUI bootstrap).
    """
    raw = (os.getenv("SOVEREIGN_MCP_SERVERS") or "").strip()
    if not raw:
        return 0
    try:
        specs = json.loads(raw)
    except ValueError as e:
        logger.warning("MCP live: SOVEREIGN_MCP_SERVERS is not valid JSON: %s", e)
        return 0
    if not isinstance(specs, list):
        return 0
    connected = 0
    for i, spec in enumerate(specs):
        if not isinstance(spec, dict):
            continue
        server_id = str(spec.get("id") or f"mcp-{i+1}")
        try:
            if spec.get("transport") == "http" and spec.get("url"):
                client = MCPClient(transport="http", url=str(spec["url"]))
            elif spec.get("command"):
                cmd = spec["command"]
                client = MCPClient(transport="stdio", command=cmd if isinstance(cmd, list) else [str(cmd)])
            else:
                logger.warning("MCP live: server '%s' has neither command nor url; skipped.", server_id)
                continue
            await client.connect()
            tools = await client.list_tools()
            register_client(server_id, client, tools=tools)
            connected += 1
        except Exception as e:  # noqa: BLE001 - a bad server must not stop startup
            logger.warning("MCP live: failed to connect server '%s': %s", server_id, e)
    return connected


async def mcp_tool_handlers() -> tuple[dict[str, Callable[[dict], Any]], dict[str, str]]:
    """
    Build (handlers, descriptions) for every tool on every registered MCP server.

    Handlers are async callables `(args) -> str` (the worker loop awaits them). On a
    name collision the first server wins; built-in worker tools still take precedence
    because callers merge these with `setdefault`. Returns empty dicts when no servers
    are registered (default) — a no-op.
    """
    handlers: dict[str, Callable[[dict], Any]] = {}
    descriptions: dict[str, str] = {}
    for server_id in list(_clients):
        client = _clients[server_id]
        for schema in await _ensure_tools(server_id):
            name = schema.name
            if not name or name in handlers:
                continue
            handlers[name] = _make_handler(client, name)
            descriptions[name] = (schema.description or f"MCP tool from '{server_id}'")[:200]
    return handlers, descriptions
