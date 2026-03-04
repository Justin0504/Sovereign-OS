"""
MCP (Model Context Protocol) client and tool mapping for Sovereign-OS.
"""

from sovereign_os.mcp.client import MCPClient, MCPToolSchema
from sovereign_os.mcp.tool_mapping import get_tools_for_skill, skill_tool_map

__all__ = [
    "MCPClient",
    "MCPToolSchema",
    "get_tools_for_skill",
    "skill_tool_map",
]
