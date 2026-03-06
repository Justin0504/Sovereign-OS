"""
MCPWorker: Worker that executes tasks by calling MCP tools.

Phase 5 self-hiring: when a skill has no registered Worker but the MCP tool graph
has tools for that skill, the registry can instantiate MCPWorker to fulfill the task.
"""

from __future__ import annotations

import logging
from typing import Callable

from sovereign_os.agents.base import BaseWorker, TaskInput, TaskResult
from sovereign_os.mcp.client import MCPToolSchema

logger = logging.getLogger(__name__)

# Common tool argument keys by tool name (description -> arg). Fallback: "query" or "input".
_TOOL_ARG_KEY: dict[str, str] = {
    "search": "query",
    "read_file": "path",
    "write_file": "path",
    "fetch_url": "url",
    "run_terminal_cmd": "command",
    "list_dir": "path",
}


def _args_for_tool(tool_name: str, task_input: TaskInput) -> dict[str, str]:
    """Build minimal arguments for an MCP tool call from task input."""
    key = _TOOL_ARG_KEY.get(tool_name, "query")
    if key == "path" and task_input.description:
        # Use first line or first word as path hint if description looks like a path
        desc = task_input.description.strip()
        if "/" in desc or desc.endswith(".md") or desc.endswith(".txt"):
            return {key: desc.split()[0] if desc.split() else desc}
    return {key: task_input.description or task_input.task_id}


class MCPWorker(BaseWorker):
    """
    Worker that runs a task by invoking one or more MCP tools.
    Configured with (skill, list of (server_id, tool_schema)); uses the first
    available tool with task description as input (e.g. query/path).
    """

    def __init__(
        self,
        agent_id: str,
        system_prompt: str = "",
        *,
        skill: str = "",
        tools: list[tuple[str, MCPToolSchema]] | None = None,
        get_client: Callable[[str], object] | None = None,
        llm: object = None,
    ) -> None:
        super().__init__(agent_id=agent_id, system_prompt=system_prompt, llm=llm)
        self._skill = skill
        self._tools = tools or []
        self._get_client = get_client

    async def execute(self, task: TaskInput) -> TaskResult:
        if not self._tools or not self._get_client:
            return TaskResult(
                task_id=task.task_id,
                success=False,
                output="[MCPWorker] No tools or client configured.",
                metadata={"worker": "MCPWorker"},
            )
        server_id, schema = self._tools[0]
        client = self._get_client(server_id)
        if not hasattr(client, "call_tool"):
            return TaskResult(
                task_id=task.task_id,
                success=False,
                output="[MCPWorker] Invalid MCP client.",
                metadata={"worker": "MCPWorker"},
            )
        args = _args_for_tool(schema.name, task)
        try:
            result = await client.call_tool(schema.name, args)
            content = result.get("content", [])
            if isinstance(content, list) and content:
                text = content[0].get("text", str(content))
            else:
                text = str(result)
            return TaskResult(
                task_id=task.task_id,
                success=not result.get("isError", False),
                output=text[:65536] if len(text) > 65536 else text,
                metadata={"worker": "MCPWorker", "tool": schema.name, "server": server_id},
            )
        except Exception as e:
            logger.exception("MCPWorker execute failed: %s", e)
            return TaskResult(
                task_id=task.task_id,
                success=False,
                output=f"[MCPWorker] Tool call failed: {e}",
                metadata={"worker": "MCPWorker", "error": str(e)},
            )
