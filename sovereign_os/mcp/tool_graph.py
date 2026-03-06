"""
MCP Tool Graph: discover tools from MCP servers and map them to skills for self-hiring.

Phase 5: Multi-agent self-hiring from MCP tool graph.
Registry can resolve a required_skill to available MCP tools and instantiate MCPWorker.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sovereign_os.mcp.client import MCPClient, MCPToolSchema
from sovereign_os.mcp.tool_mapping import skill_tool_map

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    pass


class MCPToolGraph:
    """
    Discovers and caches tools from registered MCP servers.
    Maps skills (from TaskPlan) to available tools for dynamic worker resolution.
    """

    def __init__(self) -> None:
        # server_id -> (client, {tool_name: MCPToolSchema})
        self._servers: dict[str, tuple[MCPClient, dict[str, MCPToolSchema]]] = {}

    async def add_server(self, server_id: str, client: MCPClient) -> None:
        """
        Register an MCP server: connect and cache its tool list.
        Call after client.connect() if using stdio.
        """
        try:
            tools_list = await client.list_tools()
        except Exception as e:
            logger.warning("MCP tool graph: list_tools failed for %s: %s", server_id, e)
            tools_list = []
        by_name = {t.name: t for t in tools_list}
        self._servers[server_id] = (client, by_name)
        logger.info(
            "MCP tool graph: registered server %s with %d tools",
            server_id,
            len(by_name),
        )

    def get_tools_for_skill(self, required_skill: str) -> list[tuple[str, MCPToolSchema]]:
        """
        Return (server_id, schema) for each tool that matches the skill.
        Uses skill_tool_map to know which tool names belong to the skill;
        returns only tools that exist in at least one registered server.
        """
        key = required_skill.strip().lower()
        tool_names = skill_tool_map.get(key, [])
        if not tool_names:
            return []
        result: list[tuple[str, MCPToolSchema]] = []
        for tool_name in tool_names:
            for server_id, (_client, tools) in self._servers.items():
                if tool_name in tools:
                    result.append((server_id, tools[tool_name]))
                    break
        return result

    def get_client(self, server_id: str) -> MCPClient:
        """Return the MCP client for the given server (for tool calls)."""
        if server_id not in self._servers:
            raise KeyError(f"MCP server not registered: {server_id}")
        return self._servers[server_id][0]

    def discover_skills(self) -> set[str]:
        """
        Return the set of skills that have at least one available tool
        in the current tool graph (for self-hiring / fallback).
        """
        available_tool_names: set[str] = set()
        for _client, tools in self._servers.values():
            available_tool_names.update(tools.keys())
        return {
            skill
            for skill, names in skill_tool_map.items()
            if any(t in available_tool_names for t in names)
        }

    def has_tools_for_skill(self, required_skill: str) -> bool:
        """True if at least one tool is available for this skill."""
        return len(self.get_tools_for_skill(required_skill)) > 0
