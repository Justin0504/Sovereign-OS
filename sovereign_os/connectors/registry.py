"""
Connector registry — the tools each delivery category needs to do top-tier work,
and whether they're configured. A category (e.g. research) declares connectors
(web_search, web_fetch); this registry says what each connector is, how it's
provided (MCP server / built-in / HTTP), and whether the environment has it.

How tools reach workers: MCP-kind connectors are fulfilled through the existing
self-hiring path — when an MCPToolGraph exposes tools for a skill, the registry
bids an `mcp-{skill}` worker (see WorkerRegistry / mcp/). This registry is the
catalog + readiness layer on top of that.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from sovereign_os.agents.categories import CATEGORIES, TaskCategory, get_category


@dataclass(frozen=True)
class ConnectorSpec:
    name: str
    kind: str                     # "mcp" | "builtin" | "http"
    description: str
    env_keys: tuple[str, ...] = field(default_factory=tuple)  # env needed to be "available"
    mcp_server: str = ""          # MCP server hint for self-hiring


CONNECTORS: dict[str, ConnectorSpec] = {
    "web_search": ConnectorSpec("web_search", "mcp", "Search the web for current info.", mcp_server="search"),
    "web_fetch": ConnectorSpec("web_fetch", "builtin", "Fetch and read a URL.", ()),
    "send_email": ConnectorSpec("send_email", "builtin", "Send email via SMTP.",
                                ("SOVEREIGN_SMTP_HOST", "SOVEREIGN_SMTP_USER")),
    "git": ConnectorSpec("git", "mcp", "Clone/read a git repo.", mcp_server="git"),
    "file_read": ConnectorSpec("file_read", "builtin", "Read provided files.", ()),
    "code_search": ConnectorSpec("code_search", "mcp", "Search a codebase.", mcp_server="code"),
    "sql": ConnectorSpec("sql", "mcp", "Query a SQL database.", ("DATABASE_URL",), mcp_server="sql"),
    "spreadsheet": ConnectorSpec("spreadsheet", "builtin", "Parse CSV/XLSX data.", ()),
    "figma": ConnectorSpec("figma", "mcp", "Read/write Figma design files.", ("FIGMA_TOKEN",), mcp_server="figma"),
    "image_gen": ConnectorSpec("image_gen", "http", "Generate images from a prompt.", ("IMAGE_GEN_API_KEY",)),
    "workflow": ConnectorSpec("workflow", "mcp", "Trigger automation workflows.", mcp_server="workflow"),
    "webhook": ConnectorSpec("webhook", "builtin", "POST to a webhook URL.", ("SOVEREIGN_WEBHOOK_URL",)),
}


def get_connector(name: str) -> ConnectorSpec | None:
    return CONNECTORS.get((name or "").strip().lower())


def dispatch(name: str, **kwargs):
    """
    Invoke a built-in connector that has a real handler (e.g. send_email).
    Returns the handler result, or {"error": ...} when the connector has no
    built-in handler (mcp/http connectors are reached via their own paths).
    """
    key = (name or "").strip().lower()
    if key == "send_email":
        from sovereign_os.connectors.email_connector import send_email
        return send_email(kwargs.get("to", ""), kwargs.get("subject", ""), kwargs.get("body", ""),
                          live=kwargs.get("live"))
    return {"error": f"no built-in handler for connector '{name}'"}


def is_available(spec: ConnectorSpec) -> bool:
    """A connector is available when all its required env keys are set (built-ins with no keys are always available)."""
    return all(os.getenv(k) for k in spec.env_keys)


def connectors_for_category(category: TaskCategory | str) -> list[ConnectorSpec]:
    cat = category if isinstance(category, TaskCategory) else get_category(category)
    return [CONNECTORS[n] for n in cat.connectors if n in CONNECTORS]


def readiness_for_category(category: TaskCategory | str) -> dict[str, bool]:
    """Map each of a category's connectors to whether it's configured/available."""
    return {spec.name: is_available(spec) for spec in connectors_for_category(category)}


def required_mcp_servers() -> set[str]:
    """MCP servers referenced by any category's connectors — what to stand up for full coverage."""
    needed: set[str] = set()
    for cat in CATEGORIES:
        for spec in connectors_for_category(cat):
            if spec.kind == "mcp" and spec.mcp_server:
                needed.add(spec.mcp_server)
    return needed


def coverage_report() -> dict[str, dict]:
    """Per-category connector readiness, for ops/dashboards."""
    out: dict[str, dict] = {}
    for cat in CATEGORIES:
        specs = connectors_for_category(cat)
        out[cat.key] = {
            "connectors": [s.name for s in specs],
            "available": [s.name for s in specs if is_available(s)],
            "missing": [s.name for s in specs if not is_available(s)],
        }
    return out
