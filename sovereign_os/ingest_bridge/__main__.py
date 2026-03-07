"""
Run the ingest bridge: python -m sovereign_os.ingest_bridge
Serves on BRIDGE_PORT (default 9000). Set SOVEREIGN_INGEST_URL to http://host:9000/jobs?take=true
so Sovereign-OS consumes jobs on each poll (optional take=true to avoid re-sending same jobs).
"""

from __future__ import annotations

import logging
import os
import sys

import uvicorn

from sovereign_os.ingest_bridge.config import BridgeConfig

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)

def main() -> None:
    cfg = BridgeConfig.from_env()
    if cfg.mode == "post":
        # Post mode: no HTTP server; run loop in main thread (blocking)
        from sovereign_os.ingest_bridge.runner import _run_once, _sources_from_config
        from sovereign_os.ingest_bridge.dedup import Deduplicator
        import time
        dedup = Deduplicator(window_sec=cfg.dedup_window_sec)
        sources = _sources_from_config(cfg)
        while True:
            _run_once(cfg, dedup, sources)
            time.sleep(cfg.poll_interval_sec)
    else:
        host = cfg.host
        port = cfg.port
        uvicorn.run(
            "sovereign_os.ingest_bridge.app:app",
            host=host,
            port=port,
            log_level=os.getenv("LOG_LEVEL", "info").lower(),
        )


if __name__ == "__main__":
    main()
