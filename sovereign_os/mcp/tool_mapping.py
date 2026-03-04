"""
Map MCP tool schemas to Worker capabilities by required_skill.
When a Worker is instantiated, it can be "mounted" with relevant MCP tools for its skill.
"""

from typing import Any

# Default mapping: skill (lowercase) -> list of MCP tool names to mount for that skill.
# Extend or override when connecting to real MCP servers (Filesystem, GitHub, Google Search, etc.).
skill_tool_map: dict[str, list[str]] = {
    "research": ["read_file", "search", "fetch_url"],
    "code": ["read_file", "write_file", "run_terminal_cmd", "list_dir"],
    "audit": ["read_file", "search"],
    "content": ["read_file", "write_file", "search"],
    "analytics": ["read_file", "search", "run_terminal_cmd"],
}


def get_tools_for_skill(required_skill: str) -> list[str]:
    """
    Return the list of MCP tool names to mount for the given required_skill.
    Used when instantiating a Worker so it can be given access to relevant tools.
    """
    key = required_skill.strip().lower()
    return list(skill_tool_map.get(key, []))


def register_skill_tools(skill: str, tool_names: list[str]) -> None:
    """Register or override MCP tools for a skill."""
    key = skill.strip().lower()
    skill_tool_map[key] = list(tool_names)
