"""
RentAHuman client: the outbound side of the oversight system — Sovereign-OS
posts a task, funds escrow, and (after the human delivers) releases or disputes.

API (https://rentahuman.ai/api, header X-API-Key: rah_live_...):
  POST /bounties                 create a bounty (title, description, price USD, ...)
  POST /escrow/checkout          fund escrow for a bounty -> Stripe checkout
  GET  /escrow/agent-rentals     list rentals + next actions
  GET  /escrow/:id               escrow status (pending|funded|delivered|completed|...)
  POST /escrow/:id/complete      mark delivered work accepted
  POST /escrow/:id/release       release escrow to worker
  POST /escrow/:id/dispute       freeze for review
  POST /escrow/:id/cancel        cancel + refund

Money-moving calls (fund/complete/release/dispute/cancel) are gated behind
`live=True`; in dry-run (default) they return a deterministic simulated response
and move no funds, so the whole oversight loop can run without an account.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://rentahuman.ai/api"


def _http_get_json(url: str, params: dict, headers: dict, timeout: float) -> Any:
    import requests  # type: ignore[import]

    r = requests.get(url, params=params, headers=headers, timeout=timeout)
    r.raise_for_status()
    return r.json()


def _http_post_json(url: str, body: dict, headers: dict, timeout: float) -> Any:
    import requests  # type: ignore[import]

    r = requests.post(url, json=body, headers=headers, timeout=timeout)
    r.raise_for_status()
    return r.json()


class RentAHumanClient:
    """REST client for RentAHuman. Writes are dry-run unless `live=True`."""

    def __init__(
        self,
        api_key: str = "",
        *,
        base_url: str = DEFAULT_BASE_URL,
        live: bool = False,
        timeout: float = 15.0,
        get_json: Callable[..., Any] | None = None,
        post_json: Callable[..., Any] | None = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.live = live
        self.timeout = timeout
        self._get_json = get_json or _http_get_json
        self._post_json = post_json or _http_post_json

    @property
    def _headers(self) -> dict[str, str]:
        return {"X-API-Key": self.api_key} if self.api_key else {}

    def _sim_id(self, kind: str, seed: str) -> str:
        digest = hashlib.sha256(f"{kind}|{seed}".encode()).hexdigest()[:16]
        return f"sim_{kind}_{digest}"

    # ------------------------------------------------------------- writes
    def post_bounty(
        self,
        *,
        title: str,
        description: str,
        price_cents: int,
        completion_criteria: str = "",
        evidence_types: list[str] | None = None,
        estimated_hours: float | None = None,
        category: str | None = None,
        deadline: str | None = None,
    ) -> dict[str, Any]:
        body = {
            "title": title[:200],
            "description": description[:5000],
            "completionCriteria": completion_criteria,
            "evidenceTypes": evidence_types or ["text", "link"],
            "priceType": "fixed",
            "price": round(price_cents / 100.0, 2),
        }
        if estimated_hours is not None:
            body["estimatedHours"] = estimated_hours
        if category:
            body["category"] = category
        if deadline:
            body["deadline"] = deadline
        if not self.live:
            bid = self._sim_id("bounty", f"{title}|{price_cents}")
            logger.info("RENTAHUMAN DRY-RUN: would post bounty %s ($%.2f).", bid, price_cents / 100.0)
            return {"success": True, "id": bid, "status": "pending", "dry_run": True, **body}
        return self._post_json(f"{self.base_url}/bounties", body, self._headers, self.timeout)

    def fund_escrow(self, bounty_id: str, amount_cents: int) -> dict[str, Any]:
        """Money-moving: fund escrow for a bounty (Stripe checkout in live mode)."""
        if not self.live:
            eid = self._sim_id("escrow", bounty_id)
            logger.info("RENTAHUMAN DRY-RUN: would fund escrow %s with $%.2f.", eid, amount_cents / 100.0)
            return {"success": True, "id": eid, "status": "funded", "amount_cents": amount_cents, "dry_run": True}
        self._require_key()
        body = {"bountyId": bounty_id, "amount": round(amount_cents / 100.0, 2)}
        return self._post_json(f"{self.base_url}/escrow/checkout", body, self._headers, self.timeout)

    def complete(self, escrow_id: str) -> dict[str, Any]:
        """Mark delivered work accepted (quality gate passed)."""
        return self._write(f"/escrow/{escrow_id}/complete", escrow_id, "complete", "completed")

    def release(self, escrow_id: str) -> dict[str, Any]:
        """Release escrow funds to the worker."""
        return self._write(f"/escrow/{escrow_id}/release", escrow_id, "release", "released")

    def dispute(self, escrow_id: str) -> dict[str, Any]:
        """Freeze escrow for review (quality gate failed)."""
        return self._write(f"/escrow/{escrow_id}/dispute", escrow_id, "dispute", "disputed")

    def cancel(self, escrow_id: str) -> dict[str, Any]:
        """Cancel escrow and refund."""
        return self._write(f"/escrow/{escrow_id}/cancel", escrow_id, "cancel", "cancelled")

    def _write(self, path: str, escrow_id: str, action: str, sim_status: str) -> dict[str, Any]:
        if not self.live:
            logger.info("RENTAHUMAN DRY-RUN: would %s escrow %s.", action, escrow_id)
            return {"success": True, "id": escrow_id, "status": sim_status, "action": action, "dry_run": True}
        self._require_key()
        return self._post_json(f"{self.base_url}{path}", {}, self._headers, self.timeout)

    # -------------------------------------------------------------- reads
    def list_rentals(self) -> Any:
        if not self.live:
            return []
        self._require_key()
        return self._get_json(f"{self.base_url}/escrow/agent-rentals", {}, self._headers, self.timeout)

    def get_escrow(self, escrow_id: str) -> dict[str, Any]:
        if not self.live:
            return {"id": escrow_id, "status": "delivered", "dry_run": True}
        self._require_key()
        return self._get_json(f"{self.base_url}/escrow/{escrow_id}", {}, self._headers, self.timeout)

    def _require_key(self) -> None:
        if not self.api_key:
            raise ValueError("RENTAHUMAN_API_KEY required for live calls.")
