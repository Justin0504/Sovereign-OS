"""
Industrial-grade ingest bridge: pull orders from Reddit, scrapers, retail APIs
and feed Sovereign-OS via HTTP (SOVEREIGN_INGEST_URL) or direct POST /api/jobs.

Run: python -m sovereign_os.ingest_bridge
Config: env vars or BRIDGE_CONFIG_PATH YAML. See docs/INGEST_BRIDGE.md.
"""

from sovereign_os.ingest_bridge.config import BridgeConfig

__all__ = ["BridgeConfig"]
