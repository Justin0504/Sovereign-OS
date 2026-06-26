"""
ClawTasks source: pull open, funded bounties from the ClawTasks agent-to-agent
bounty marketplace (https://clawtasks.com/api) into the Sovereign-OS job queue.

API shape (public, no auth needed for discovery):
  GET /bounties?status=open&min=&max=&tags=  -> list of bounties
  bounty fields: id, title, description, amount (USDC), currency, status,
                 mode (instant|proposal|race|contest), funded (bool),
                 deadline_hours, tags[], poster, assigned_to

Two layers, separated by money risk:
  - ClawTasksOrderSource  — DISCOVERY ONLY. Reads public open bounties and emits
    RawOrders. No auth, no funds moved. Safe to run continuously.
  - ClawTasksClient       — CLAIM / SUBMIT. Claiming stakes 10% of the bounty in
    USDC on Base (irreversible), so money-moving calls are gated behind `live=True`
    (env CLAWTASKS_LIVE). In dry-run (default) they log the intended action and
    return {"dry_run": True} without hitting the network.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Iterator

from sovereign_os.ingest_bridge.sources.base import RawOrder, OrderSource

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://clawtasks.com/api"


def _http_get_json(url: str, params: dict[str, Any], headers: dict[str, str], timeout: float) -> Any:
    import requests  # type: ignore[import]

    resp = requests.get(url, params=params, headers=headers, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _http_post_json(url: str, body: dict[str, Any], headers: dict[str, str], timeout: float) -> Any:
    import requests  # type: ignore[import]

    resp = requests.post(url, json=body, headers=headers, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


class ClawTasksOrderSource(OrderSource):
    """Discovery-only: map open, funded ClawTasks bounties to RawOrders."""

    source_name = "clawtasks"

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        min_amount_usd: float = 0.0,
        max_amount_usd: float = 0.0,
        tags: list[str] | None = None,
        require_funded: bool = True,
        skip_assigned: bool = True,
        limit: int = 50,
        charter: str = "Default",
        timeout: float = 15.0,
        get_json: Callable[..., Any] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.min_amount_usd = max(0.0, min_amount_usd)
        self.max_amount_usd = max(0.0, max_amount_usd)
        self.tags = [t.strip() for t in (tags or []) if t.strip()]
        self.require_funded = require_funded
        self.skip_assigned = skip_assigned
        self.limit = max(1, limit)
        self.charter = charter or "Default"
        self.timeout = timeout
        self._get_json = get_json or _http_get_json

    def _list_open_bounties(self) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"status": "open"}
        if self.min_amount_usd > 0:
            params["min"] = self.min_amount_usd
        if self.max_amount_usd > 0:
            params["max"] = self.max_amount_usd
        if self.tags:
            params["tags"] = ",".join(self.tags)
        data = self._get_json(f"{self.base_url}/bounties", params, {}, self.timeout)
        # API may return a bare list or {"bounties": [...]}
        if isinstance(data, dict):
            data = data.get("bounties") or data.get("data") or []
        return data if isinstance(data, list) else []

    def _accept(self, b: dict[str, Any]) -> bool:
        if (b.get("status") or "open").lower() != "open":
            return False
        if self.require_funded and not b.get("funded", False):
            return False
        if self.skip_assigned and (b.get("assigned_to") or "").strip():
            return False
        amount = float(b.get("amount") or 0)
        if self.min_amount_usd > 0 and amount < self.min_amount_usd:
            return False
        if self.max_amount_usd > 0 and amount > self.max_amount_usd:
            return False
        return True

    def fetch(self) -> Iterator[RawOrder]:
        try:
            bounties = self._list_open_bounties()
        except Exception as e:
            logger.warning("ClawTasks source: fetch failed: %s", e)
            return
        emitted = 0
        for b in bounties:
            if emitted >= self.limit:
                break
            if not isinstance(b, dict) or not self._accept(b):
                continue
            bid = str(b.get("id") or "").strip()
            if not bid:
                continue
            title = (b.get("title") or "").strip()
            description = (b.get("description") or "").strip()
            goal = (f"{title}\n\n{description}" if description else title)[:20_000]
            amount_cents = int(round(float(b.get("amount") or 0) * 100))
            yield RawOrder(
                source_id=f"clawtasks:{bid}",
                goal=goal,
                amount_cents=amount_cents,
                currency=(b.get("currency") or "USDC"),
                charter=self.charter,
                meta={
                    "bounty_type": b.get("bounty_type") or "standard",
                    "mode": b.get("mode") or "instant",
                    "deadline_hours": b.get("deadline_hours"),
                    "tags": b.get("tags") or [],
                    "poster": b.get("poster") or "",
                    "funded": bool(b.get("funded", False)),
                },
                contact={
                    "platform": "clawtasks",
                    "bounty_id": bid,
                    "mode": b.get("mode") or "instant",
                    "deadline_hours": b.get("deadline_hours"),
                },
            )
            emitted += 1
        logger.info("ClawTasks source: emitted %d order(s) from %d open bounties", emitted, len(bounties))


class ClawTasksClient:
    """
    Claim/submit work on ClawTasks. Money-moving calls (claim/submit) are gated:
    in dry-run (default) they log and return {"dry_run": True} without any network
    call. Set live=True (env CLAWTASKS_LIVE=true) only with a funded Base wallet.
    """

    def __init__(
        self,
        api_key: str,
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
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    def pending(self) -> Any:
        """Read-only: bounties awaiting this agent's action. Safe even when live."""
        if not self.api_key:
            logger.warning("ClawTasks client: CLAWTASKS_API_KEY required for pending().")
            return []
        return self._get_json(f"{self.base_url}/agents/me/pending", {}, self._headers, self.timeout)

    def claim(self, bounty_id: str) -> dict[str, Any]:
        """Claim a bounty (instant mode). Stakes 10% USDC on-chain — gated by `live`."""
        if not self.live:
            logger.warning("ClawTasks DRY-RUN: would claim bounty %s (set CLAWTASKS_LIVE=true to stake).", bounty_id)
            return {"dry_run": True, "action": "claim", "bounty_id": bounty_id}
        if not self.api_key:
            raise ValueError("CLAWTASKS_API_KEY required to claim live.")
        return self._post_json(f"{self.base_url}/bounties/{bounty_id}/claim", {}, self._headers, self.timeout)

    def submit(self, bounty_id: str, content: str) -> dict[str, Any]:
        """Submit completed work (up to 50k chars) — gated by `live`."""
        if not self.live:
            logger.warning("ClawTasks DRY-RUN: would submit %d chars to bounty %s.", len(content or ""), bounty_id)
            return {"dry_run": True, "action": "submit", "bounty_id": bounty_id}
        if not self.api_key:
            raise ValueError("CLAWTASKS_API_KEY required to submit live.")
        body = {"content": (content or "")[:50_000]}
        return self._post_json(f"{self.base_url}/bounties/{bounty_id}/submit", body, self._headers, self.timeout)
