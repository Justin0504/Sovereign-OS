"""
StacksTasker client for the outbound oversight broker.

StacksTasker (https://stackstasker.com, Stacks testnet, rewards in STX) is an
agent-to-agent market: a poster creates a task with a bounty, workers BID, and
settlement happens on-chain via the winning bid. There is no poster-controlled
release/dispute escrow endpoint (unlike RentAHuman), so:

  - post_bounty   -> real POST /tasks (the budget gate fully applies here).
  - fund_escrow   -> no-op: the STX bounty is committed at task creation.
  - complete/release/dispute/cancel -> NOT applicable as a poster API; these are
    logged no-ops. StacksTasker settles on-chain through bids, so the broker's
    quality-gate *release* is RentAHuman-specific and does not move STX here.

Amounts are STX (testnet) — treated as nominal units, never USD-converted.
Writes are dry-run unless `live=True`.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://stackstasker.com"


def _http_post_json(url: str, body: dict, headers: dict, timeout: float) -> Any:
    import requests  # type: ignore[import]

    r = requests.post(url, json=body, headers=headers, timeout=timeout)
    r.raise_for_status()
    return r.json()


class StacksTaskerClient:
    """EscrowClient-compatible client for StacksTasker (budget-gated posting only)."""

    def __init__(
        self,
        poster_address: str = "",
        *,
        base_url: str = DEFAULT_BASE_URL,
        category: str = "general",
        live: bool = False,
        timeout: float = 15.0,
        post_json: Callable[..., Any] | None = None,
    ) -> None:
        self.poster_address = poster_address
        self.base_url = base_url.rstrip("/")
        self.category = category
        self.live = live
        self.timeout = timeout
        self._post_json = post_json or _http_post_json

    def post_bounty(self, *, title: str, description: str, price_cents: int, completion_criteria: str = "") -> dict[str, Any]:
        """Create a StacksTasker task. `price_cents` is interpreted as STX (nominal)."""
        bounty_stx = round(price_cents / 100.0, 6)
        body = {
            "title": title[:200],
            "description": (description + (f"\n\nCompletion: {completion_criteria}" if completion_criteria else ""))[:5000],
            "category": self.category,
            "bounty": str(bounty_stx),
            "currency": "STX",
            "posterAddress": self.poster_address,
        }
        if not self.live:
            tid = "sim_st_" + hashlib.sha256(f"{title}|{price_cents}".encode()).hexdigest()[:14]
            logger.info("STACKSTASKER DRY-RUN: would post task %s (%.6f STX).", tid, bounty_stx)
            return {"id": tid, "status": "open", "dry_run": True, **body}
        if not self.poster_address:
            raise ValueError("poster_address (Stacks wallet) required for live StacksTasker posting.")
        return self._post_json(f"{self.base_url}/tasks?currency=STX", body, {}, self.timeout)

    def fund_escrow(self, bounty_id: str, amount_cents: int) -> dict[str, Any]:
        """No-op: the STX bounty is committed at task creation; escrow id == task id."""
        return {"id": bounty_id, "status": "funded", "note": "stx committed at creation"}

    # StacksTasker settles on-chain via bids — poster has no release/dispute API.
    def complete(self, escrow_id: str) -> dict[str, Any]:
        return self._unsupported(escrow_id, "complete")

    def release(self, escrow_id: str) -> dict[str, Any]:
        return self._unsupported(escrow_id, "release")

    def dispute(self, escrow_id: str) -> dict[str, Any]:
        return self._unsupported(escrow_id, "dispute")

    def cancel(self, escrow_id: str) -> dict[str, Any]:
        return self._unsupported(escrow_id, "cancel")

    def get_escrow(self, escrow_id: str) -> dict[str, Any]:
        # No poster-side escrow status; report 'open' so the poller doesn't try to settle.
        return {"id": escrow_id, "status": "open", "note": "on-chain bid settlement"}

    def _unsupported(self, escrow_id: str, action: str) -> dict[str, Any]:
        logger.info("STACKSTASKER: %s not applicable for %s (on-chain bid settlement).", action, escrow_id)
        return {"id": escrow_id, "status": "open", "unsupported": True, "action": action}
