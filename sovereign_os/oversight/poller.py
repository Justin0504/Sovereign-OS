"""
Delivery poller: close the outbound oversight loop automatically.

For each funded escrow, check the platform-side status; once a worker has
delivered, run the Auditor quality gate (release on pass, dispute on fail). This
turns post -> wait -> verify -> pay/withhold into a hands-off loop.

In dry-run the client reports escrows as "delivered", so a single poll settles
them against a simulated deliverable — the loop is observable without funds.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

READY_STATUSES = ("delivered",)


async def poll_and_settle(
    broker: Any,
    registry: Any,
    *,
    deliverable_for: Callable[[Any], str] | None = None,
    ready_statuses: tuple[str, ...] = READY_STATUSES,
) -> list[dict[str, Any]]:
    """
    Settle every funded escrow whose platform status is now 'delivered'.

    `deliverable_for(record) -> str` supplies the delivered work to audit; by
    default a placeholder is used (real integrations fetch it from the platform).
    Returns a list of {escrow_id, action, paid, score} for what was settled.
    """
    settled: list[dict[str, Any]] = []
    for rec in registry.list(status="funded"):
        status = broker.escrow_status(rec.escrow_id)
        if status not in ready_statuses:
            continue
        deliverable = deliverable_for(rec) if deliverable_for else f"[delivered work] {rec.title}"
        res = await broker.review_and_settle(
            escrow_id=rec.escrow_id,
            deliverable=deliverable,
            task_description=rec.title,
            price_cents=rec.price_cents,
            required_skill=rec.required_skill,
            completion_criteria=rec.completion_criteria,
        )
        settled.append({
            "escrow_id": rec.escrow_id,
            "action": res["action"],
            "paid": res["paid"],
            "score": res["score"],
        })
        logger.info("OVERSIGHT POLLER: settled %s -> %s (paid=%s)", rec.escrow_id, res["action"], res["paid"])
    return settled
