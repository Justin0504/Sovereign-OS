"""
Generic MCP (Model Context Protocol) client for local or remote MCP servers.
JSON-RPC 2.0 over stdio or HTTP; tools/list and tools/call.
"""

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class MCPToolSchema:
    """Schema of an MCP tool (name, description, inputSchema)."""

    name: str
    description: str = ""
    input_schema: dict[str, Any] | None = None


class MCPClient:
    """
    Generic MCP client: connect to local (stdio) or remote (HTTP) MCP servers,
    list tools, and call tools. Thread-safe per connection.
    """

    def __init__(self, *, transport: str = "stdio", command: list[str] | None = None, url: str | None = None) -> None:
        if transport == "stdio" and command:
            self._transport = "stdio"
            self._command = command
            self._url = None
        elif transport == "http" and url:
            self._transport = "http"
            self._command = None
            self._url = url
        else:
            raise ValueError("Provide command (stdio) or url (http)")
        self._process: asyncio.subprocess.Process | None = None
        self._request_id = 0
        self._lock = asyncio.Lock()

    async def _next_id(self) -> int:
        async with self._lock:
            self._request_id += 1
            return self._request_id

    async def _send_stdio(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if self._process is None or self._process.stdin is None:
            raise RuntimeError("MCP client not connected (stdio)")
        req = {"jsonrpc": "2.0", "id": await self._next_id(), "method": method, "params": params or {}}
        payload = json.dumps(req) + "\n"
        self._process.stdin.write(payload.encode())
        await self._process.stdin.drain()
        line = (await self._process.stdout.readline()).decode().strip()
        if not line:
            raise RuntimeError("MCP server closed connection")
        out = json.loads(line)
        if "error" in out:
            raise RuntimeError(f"MCP error: {out['error']}")
        return out.get("result", {})

    async def connect(self) -> None:
        """Establish connection (start subprocess for stdio, or session for HTTP)."""
        if self._transport == "stdio":
            self._process = await asyncio.create_subprocess_exec(
                *self._command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            logger.info("MCP client connected (stdio): %s", self._command)
        else:
            # HTTP: store URL; actual request in _send_http (e.g. aiohttp)
            logger.info("MCP client configured (http): %s", self._url)

    async def disconnect(self) -> None:
        if self._process is not None:
            try:
                self._process.terminate()
                await asyncio.wait_for(self._process.wait(), timeout=5.0)
            except Exception:
                self._process.kill()
            self._process = None

    async def list_tools(self) -> list[MCPToolSchema]:
        """Call tools/list and return tool schemas."""
        if self._transport == "stdio":
            result = await self._send_stdio("tools/list")
        else:
            result = await self._send_http("tools/list", {})
        tools = result.get("tools", [])
        return [
            MCPToolSchema(
                name=t.get("name", ""),
                description=t.get("description", ""),
                input_schema=t.get("inputSchema"),
            )
            for t in tools
        ]

    async def _send_http(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """HTTP transport: POST JSON-RPC to self._url."""
        try:
            import aiohttp
        except ImportError:
            raise ImportError("aiohttp required for MCP HTTP transport; pip install aiohttp")
        req = {"jsonrpc": "2.0", "id": await self._next_id(), "method": method, "params": params}
        async with aiohttp.ClientSession() as session:
            async with session.post(self._url, json=req) as resp:
                data = await resp.json()
        if "error" in data:
            raise RuntimeError(f"MCP error: {data['error']}")
        return data.get("result", {})

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        """Invoke tools/call; returns result with content array and isError."""
        params = {"name": name, "arguments": arguments or {}}
        if self._transport == "stdio":
            result = await self._send_stdio("tools/call", params)
        else:
            result = await self._send_http("tools/call", params)
        return result
