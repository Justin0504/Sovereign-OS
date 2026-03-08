"""
Base type for order sources: yield RawOrder for normalizer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator


@dataclass
class RawOrder:
    source_id: str
    goal: str
    amount_cents: int = 0
    currency: str = "USD"
    charter: str = "Default"
    meta: dict | None = None
    # Optional: so we can "contact" the client after delivery (e.g. Reddit reply/DM).
    contact: dict | None = None  # e.g. {"platform": "reddit", "username": "op", "post_id": "abc", "permalink": "/r/..."}


class OrderSource:
    """Override fetch() to yield RawOrders from a source."""

    source_name: str = "base"

    def fetch(self) -> Iterator[RawOrder]:
        yield from ()
