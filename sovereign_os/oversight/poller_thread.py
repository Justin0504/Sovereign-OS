"""
Background outbound poller — periodically settles delivered escrows so the
oversight loop runs hands-off (mirrors ingest/poller.py).

Opt-in via env:
  SOVEREIGN_OVERSIGHT_POLL_ENABLED=true
  SOVEREIGN_OVERSIGHT_POLL_INTERVAL_SEC=120  (default)
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from typing import Any

logger = logging.getLogger(__name__)

_stop = threading.Event()


def tick_once(broker: Any, registry: Any) -> list[dict]:
    """Run a single settle pass synchronously (wraps the async poll_and_settle)."""
    from sovereign_os.oversight.poller import poll_and_settle

    return asyncio.run(poll_and_settle(broker, registry))


def start_oversight_poller(broker: Any, registry: Any, interval_sec: int | None = None) -> threading.Thread | None:
    """
    Start a daemon thread that settles delivered escrows every interval.
    Returns None (no thread) unless SOVEREIGN_OVERSIGHT_POLL_ENABLED is truthy.
    """
    if os.getenv("SOVEREIGN_OVERSIGHT_POLL_ENABLED", "").lower() not in ("1", "true", "yes"):
        return None
    interval = interval_sec or int(os.getenv("SOVEREIGN_OVERSIGHT_POLL_INTERVAL_SEC", "120"))
    _stop.clear()

    def _loop() -> None:
        while not _stop.is_set():
            try:
                settled = tick_once(broker, registry)
                if settled:
                    logger.info("OVERSIGHT POLLER: settled %d escrow(s)", len(settled))
            except Exception as e:  # pragma: no cover - resilience
                logger.warning("OVERSIGHT POLLER: tick failed: %s", e)
            _stop.wait(interval)

    t = threading.Thread(target=_loop, daemon=True)
    t.start()
    logger.info("OVERSIGHT POLLER: started (interval=%ss)", interval)
    return t


def stop_oversight_poller() -> None:
    _stop.set()
