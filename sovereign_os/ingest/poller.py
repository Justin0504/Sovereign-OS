"""
Poll an external URL for job payloads and enqueue them.

Set SOVEREIGN_INGEST_URL to a JSON endpoint that returns a list of:
  { "goal": str, "charter": str (optional), "amount_cents": int (optional), "currency": str (optional) }
Set SOVEREIGN_INGEST_INTERVAL_SEC to poll interval (default 60).
Set SOVEREIGN_INGEST_ONCE=true for demo: fetch once, enqueue, then stop (no repeated polling).
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
    once = (os.getenv("SOVEREIGN_INGEST_ONCE", "").strip().lower() in ("1", "true", "yes", "on"))

    try:
        max_per_poll = int(os.getenv("SOVEREIGN_INGEST_MAX_JOBS_PER_POLL", "0"))
    except ValueError:
        max_per_poll = 0
    if max_per_poll > 0:
        logger.info("INGEST: max jobs per poll = %s (SOVEREIGN_INGEST_MAX_JOBS_PER_POLL)", max_per_poll)
    if once:
        logger.info("INGEST: one-shot mode (SOVEREIGN_INGEST_ONCE=true): will fetch once then stop.")

    def _loop():
        if once:
            logger.info("INGEST: one-shot fetch from %s", url)
        else:
            logger.info("INGEST: polling %s every %ss", url, interval)
        while True:
            if not once:
                time.sleep(interval)
            items = _fetch_url(url)
            enqueued = 0
            for item in items:
                if max_per_poll > 0 and enqueued >= max_per_poll:
                    logger.debug("INGEST: cap reached (%s jobs this poll), skipping rest", max_per_poll)
                    break
                if not isinstance(item, dict):
                    continue
                goal = str(item.get("goal") or "").strip()
                if not goal:
                    continue
                charter = str(item.get("charter") or "Default")
                try:
                    amount_cents = int(float(item.get("amount_cents") or 0))
                except (TypeError, ValueError):
                    amount_cents = 0
                currency = str(item.get("currency") or "USD")
                callback_url = (item.get("callback_url") or "").strip() or None
                delivery_contact = item.get("delivery_contact")
                if delivery_contact is not None and not isinstance(delivery_contact, dict):
                    delivery_contact = None
                try:
                    enqueue_fn(goal, charter, amount_cents, currency, callback_url=callback_url, delivery_contact=delivery_contact)
                    enqueued += 1
                    logger.info("INGEST: enqueued goal=%s", goal[:80])
                except Exception as e:
                    logger.exception("INGEST: enqueue failed: %s", e)
            if once:
                logger.info("INGEST: one-shot done (%s jobs enqueued). No more polling.", enqueued)
                break

    t = threading.Thread(target=_loop, daemon=True)
    t.start()
    return t
