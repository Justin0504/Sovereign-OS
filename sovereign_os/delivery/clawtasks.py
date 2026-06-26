"""
Deliver a completed job's result back to ClawTasks: claim the bounty, then submit
the work. Money-moving by nature (claiming stakes USDC on Base), so it runs in
DRY-RUN unless CLAWTASKS_LIVE=true — in dry-run it logs the intended claim/submit
and moves no funds, so the full auto-accept loop is observable end-to-end safely.

Env:
  CLAWTASKS_API_KEY   bearer token (required for live)
  CLAWTASKS_LIVE      "true" to actually claim+submit (stakes real USDC)
  CLAWTASKS_BASE_URL  override API base (default https://clawtasks.com/api)
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def deliver_result_to_clawtasks(contact: dict, result_summary: str, job_id: str) -> bool:
    """
    Claim + submit a bounty's result. Returns True if the (live or dry-run) flow ran.

    For instant-mode bounties: claim, then submit the deliverable. In dry-run both
    calls only log. Failures are caught and logged (delivery never crashes the worker).
    """
    bounty_id = (contact or {}).get("bounty_id")
    if not bounty_id:
        logger.warning("ClawTasks delivery: no bounty_id in contact for job_id=%s", job_id)
        return False

    from sovereign_os.ingest_bridge.sources.clawtasks import ClawTasksClient

    api_key = os.getenv("CLAWTASKS_API_KEY", "")
    live = os.getenv("CLAWTASKS_LIVE", "").lower() in ("1", "true", "yes")
    base_url = os.getenv("CLAWTASKS_BASE_URL", "https://clawtasks.com/api")
    client = ClawTasksClient(api_key, base_url=base_url, live=live)

    try:
        claim = client.claim(str(bounty_id))
        if claim.get("dry_run"):
            logger.info("ClawTasks delivery (DRY-RUN): would claim+submit bounty %s for job %s.", bounty_id, job_id)
        client.submit(str(bounty_id), result_summary or "")
        return True
    except Exception as e:
        logger.warning("ClawTasks delivery failed for job_id=%s bounty=%s: %s", job_id, bounty_id, e)
        return False
