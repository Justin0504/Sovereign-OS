"""
Output: serve jobs as JSON for SOVEREIGN_INGEST_URL, or POST to Sovereign-OS /api/jobs.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

logger = logging.getLogger(__name__)

# In-memory buffer for serve mode: list of job payloads (goal, amount_cents, currency, charter)
_serve_buffer: list[dict[str, Any]] = []
_buffer_lock = threading.Lock()


def buffer_append(payload: dict) -> None:
    with _buffer_lock:
        _serve_buffer.append(payload)


def buffer_take_all() -> list[dict]:
    with _buffer_lock:
        out = list(_serve_buffer)
        _serve_buffer.clear()
        return out


def buffer_snapshot() -> list[dict]:
    with _buffer_lock:
        return list(_serve_buffer)


def post_to_sovereign(url: str, api_key: str, payload: dict) -> bool:
    try:
        import urllib.request
        import json
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{url}/api/jobs",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        if api_key:
            req.add_header("X-API-Key", api_key)
            req.add_header("Authorization", f"Bearer {api_key}")
        with urllib.request.urlopen(req, timeout=30) as resp:
            return 200 <= resp.status < 300
    except Exception as e:
        logger.warning("POST %s/api/jobs failed: %s", url, e)
        return False
