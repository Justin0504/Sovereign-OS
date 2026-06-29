"""Connector catalog + readiness for delivery categories (MCP / built-in / HTTP)."""

from sovereign_os.connectors.email_connector import send_email
from sovereign_os.connectors.figma import set_figma_reader
from sovereign_os.connectors.registry import (
    CONNECTORS,
    ConnectorSpec,
    connectors_for_category,
    coverage_report,
    dispatch,
    get_connector,
    is_available,
    readiness_for_category,
    required_mcp_servers,
)

__all__ = [
    "CONNECTORS",
    "ConnectorSpec",
    "connectors_for_category",
    "coverage_report",
    "dispatch",
    "get_connector",
    "is_available",
    "readiness_for_category",
    "required_mcp_servers",
    "send_email",
    "set_figma_reader",
]
