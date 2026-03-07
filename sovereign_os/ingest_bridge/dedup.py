"""
Deduplication: avoid re-enqueueing the same order within a time window.
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict

logger = logging.getLogger(__name__)


class Deduplicator:
    def __init__(self, window_sec: int = 3600, max_size: int = 10_000):
        self.window_sec = window_sec
        self.max_size = max_size
        self._seen: OrderedDict[str, float] = OrderedDict()

    def should_emit(self, source_id: str) -> bool:
        now = time.time()
        if source_id in self._seen:
            if now - self._seen[source_id] < self.window_sec:
                return False
        while len(self._seen) >= self.max_size:
            self._seen.popitem(last=False)
        self._seen[source_id] = now
        return True
