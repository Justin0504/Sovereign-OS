"""
GenericBountySource: a field-mapped HTTP bounty source.

Different marketplaces use different JSON keys (ClawTasks `id`/`amount`,
TaskBounty `task_id`/`bounty_usd`) and endpoints. Rather than hardcode each, this
source maps any compatible JSON list feed onto a RawOrder via a `BountyFieldMap`,
so adding a platform is configuration, not new code.

`taskbounty_source()` is a ready preset for TaskBounty. Its field names are known
from the TaskBounty MCP server (`task_id`, `bounty_usd`, Bearer `tb_live_*`,
base `/api/v1`); the exact list path is overridable via TASKBOUNTY_LIST_PATH
since it is not publicly documented.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

from sovereign_os.ingest_bridge.sources.base import OrderSource, RawOrder
from sovereign_os.ingest_bridge.sources.clawtasks import _http_get_json
import logging

logger = logging.getLogger(__name__)


@dataclass
class BountyFieldMap:
    """Maps our canonical fields to a platform's JSON keys."""

    id: str = "id"
    title: str = "title"
    description: str = "description"
    amount: str = "amount"          # numeric, in `currency` units (or cents if amount_in_cents)
    amount_in_cents: bool = False   # True => `amount` field is already in cents (no x100)
    status: str = "status"
    funded: str | None = "funded"        # None => treat all as funded
    assigned_to: str | None = "assigned_to"  # None => never treat as assigned
    tags: str | None = "tags"
    list_key: str | None = None     # wrapped response key, e.g. "bounties"/"data"
    currency: str = "USDC"          # static currency label for emitted orders


class GenericBountySource(OrderSource):
    """Discovery source for any JSON bounty feed, mapped via BountyFieldMap."""

    def __init__(
        self,
        base_url: str,
        *,
        list_path: str = "/bounties",
        field_map: BountyFieldMap | None = None,
        platform: str = "generic",
        headers: dict[str, str] | None = None,
        list_params: dict[str, Any] | None = None,
        min_amount_usd: float = 0.0,
        max_amount_usd: float = 0.0,
        require_funded: bool = True,
        skip_assigned: bool = True,
        limit: int = 50,
        charter: str = "Default",
        timeout: float = 15.0,
        get_json: Callable[..., Any] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.list_path = "/" + list_path.lstrip("/")
        self.fm = field_map or BountyFieldMap()
        self.source_name = platform
        self.platform = platform
        self.headers = headers or {}
        self.list_params = list_params or {"status": "open"}
        self.min_amount_usd = max(0.0, min_amount_usd)
        self.max_amount_usd = max(0.0, max_amount_usd)
        self.require_funded = require_funded
        self.skip_assigned = skip_assigned
        self.limit = max(1, limit)
        self.charter = charter or "Default"
        self.timeout = timeout
        self._get_json = get_json or _http_get_json

    def _list(self) -> list[dict[str, Any]]:
        data = self._get_json(f"{self.base_url}{self.list_path}", self.list_params, self.headers, self.timeout)
        if isinstance(data, dict):
            key = self.fm.list_key
            data = (data.get(key) if key else None) or data.get("bounties") or data.get("data") or []
        return data if isinstance(data, list) else []

    def _accept(self, b: dict[str, Any]) -> bool:
        fm = self.fm
        status = str(b.get(fm.status, "open") or "open").lower()
        if status not in ("open", ""):
            return False
        if self.require_funded and fm.funded is not None and not b.get(fm.funded, False):
            return False
        if self.skip_assigned and fm.assigned_to is not None and str(b.get(fm.assigned_to) or "").strip():
            return False
        amount_usd = self._amount_usd(b)
        if self.min_amount_usd > 0 and amount_usd < self.min_amount_usd:
            return False
        if self.max_amount_usd > 0 and amount_usd > self.max_amount_usd:
            return False
        return True

    def _amount_usd(self, b: dict[str, Any]) -> float:
        raw = float(b.get(self.fm.amount) or 0)
        return raw / 100.0 if self.fm.amount_in_cents else raw

    def fetch(self) -> Iterator[RawOrder]:
        try:
            rows = self._list()
        except Exception as e:
            logger.warning("%s source: fetch failed: %s", self.platform, e)
            return
        fm = self.fm
        emitted = 0
        for b in rows:
            if emitted >= self.limit:
                break
            if not isinstance(b, dict) or not self._accept(b):
                continue
            bid = str(b.get(fm.id) or "").strip()
            if not bid:
                continue
            title = str(b.get(fm.title) or "").strip()
            description = str(b.get(fm.description) or "").strip()
            goal = (f"{title}\n\n{description}" if description else title)[:20_000]
            amount_cents = int(round(self._amount_usd(b) * 100))
            yield RawOrder(
                source_id=f"{self.platform}:{bid}",
                goal=goal,
                amount_cents=amount_cents,
                currency=fm.currency,
                charter=self.charter,
                meta={"platform": self.platform, "tags": (b.get(fm.tags) if fm.tags else []) or []},
                contact={"platform": self.platform, "bounty_id": bid},
            )
            emitted += 1
        logger.info("%s source: emitted %d order(s) from %d rows", self.platform, emitted, len(rows))


def stackstasker_source(
    *,
    base_url: str | None = None,
    list_path: str | None = None,
    **kwargs: Any,
) -> GenericBountySource:
    """
    StacksTasker preset, validated against the live GET /tasks?status=open endpoint
    (2026-06): records have id, title, description, category, bounty (STX string),
    status (open/completed), network, posterAddress, wrapped in {"tasks": [...]}.
    Rewards are in STX (Stacks testnet) — currency is labelled "STX" and the amount
    is mapped nominally (1 STX -> 100 units) for filtering, not USD-converted.
    Listing needs no auth.
    """
    return GenericBountySource(
        base_url=base_url or os.getenv("STACKSTASKER_API_BASE", "https://stackstasker.com"),
        list_path=list_path or os.getenv("STACKSTASKER_LIST_PATH", "/tasks"),
        list_params={"status": "open"},
        field_map=BountyFieldMap(
            id="id",
            title="title",
            description="description",
            amount="bounty",          # STX amount as string
            status="status",          # open/completed
            funded=None,              # no funded field (testnet)
            assigned_to=None,
            tags=None,
            list_key="tasks",
            currency="STX",
        ),
        platform="stackstasker",
        **kwargs,
    )


def taskbounty_source(
    api_key: str = "",
    *,
    base_url: str | None = None,
    list_path: str | None = None,
    **kwargs: Any,
) -> GenericBountySource:
    """
    TaskBounty preset, validated against the live GET /api/v1/tasks endpoint
    (2026-06): records have id, title, short_summary, bounty_cents (already cents),
    currency, status (OPEN/AWARDED/CLOSED), tags, wrapped in {"data": [...]}.
    No `funded` field — funding is implicit, so we don't filter on it.
    Listing needs no auth; a Bearer key is only used if provided.
    """
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    return GenericBountySource(
        base_url=base_url or os.getenv("TASKBOUNTY_API_BASE", "https://www.task-bounty.com/api/v1"),
        list_path=list_path or os.getenv("TASKBOUNTY_LIST_PATH", "/tasks"),
        field_map=BountyFieldMap(
            id="id",
            title="title",
            description="short_summary",
            amount="bounty_cents",
            amount_in_cents=True,
            status="status",          # OPEN/AWARDED/CLOSED — lowercased to match "open"
            funded=None,              # no funded field; funding is implicit
            assigned_to=None,
            tags="tags",
            list_key="data",
            currency="USD",
        ),
        platform="taskbounty",
        headers=headers,
        **kwargs,
    )
