"""
Poll an external URL for job payloads and enqueue them.

Set SOVEREIGN_INGEST_URL to a JSON endpoint that returns a list of:
  { "goal": str, "charter": str (optional), "amount_cents": int (optional), "currency": str (optional) }
Set SOVEREIGN_INGEST_INTERVAL_SEC to poll interval (default 60).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)


def _fetch_url(url: str) -> list[dict[str, Any]]:
    try:
        import urllib.request
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
            if isinstance(data, list):
                return data
            if isinstance(data, dict) and "jobs" in data:
                return data["jobs"]
            return []
    except Exception as e:
        logger.warning("INGEST: fetch %s failed: %s", url, e)
        return []


def start_ingest_poller(enqueue_fn: Callable[[str, str, int, str], Any]) -> threading.Thread | None:
    """
    Start a daemon thread that polls SOVEREIGN_INGEST_URL and calls enqueue_fn(goal, charter, amount_cents, currency) for each item.
    Returns the thread if started, else None.
    """
    url = os.getenv("SOVEREIGN_INGEST_URL")
    if not url:
        return None
    interval = max(10, int(os.getenv("SOVEREIGN_INGEST_INTERVAL_SEC", "60")))

    def _loop():
        logger.info("INGEST: polling %s every %ss", url, interval)
        while True:
            time.sleep(interval)
            for item in _fetch_url(url):
                if not isinstance(item, dict):
                    continue
                goal = str(item.get("goal") or "").strip()
                if not goal:
                    continue
                charter = str(item.get("charter") or "Default")
                amount_cents = int(item.get("amount_cents") or 0)
                currency = str(item.get("currency") or "USD")
                try:
                    enqueue_fn(goal, charter, amount_cents, currency)
                    logger.info("INGEST: enqueued goal=%s", goal[:80])
                except Exception as e:
                    logger.exception("INGEST: enqueue failed: %s", e)

    t = threading.Thread(target=_loop, daemon=True)
    t.start()
    return t
