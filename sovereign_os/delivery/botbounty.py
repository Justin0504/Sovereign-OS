"""
Deliver a completed job's result back to BotBounty — the integration's missing last
mile. BotBounty is discovery-live (GET /api/agent/bounties) and its API explicitly
tells agents to "use claimEndpoint to claim a bounty, then submit your solution", but
until now completed BotBounty jobs were orphaned: no claim, no payout.

Flow: claim the bounty, then submit the solution. The claim endpoint is taken from the
bounty itself (the `claim_endpoint` carried through in the delivery contact) when the
platform provides it, else constructed from the API base. Money/reputation-bearing, so
DRY-RUN unless BOTBOUNTY_LIVE=true — in dry-run it logs the intended claim/submit and
posts nothing.

The exact submit body isn't publicly documented, so it's tolerant + configurable
(BOTBOUNTY_SUBMIT_PATH); align it to the account's API when going live.

Env: BOTBOUNTY_API_KEY, BOTBOUNTY_LIVE, BOTBOUNTY_API_BASE, BOTBOUNTY_SUBMIT_PATH,
BOTBOUNTY_AGENT_ID.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def _http_post_json(url: str, body: dict, headers: dict, timeout: float):
    import requests  # type: ignore[import]

    r = requests.post(url, json=body, headers=headers, timeout=timeout)
    r.raise_for_status()
    return r.json()


def deliver_result_to_botbounty(contact: dict, result_summary: str, job_id: str, *, post_json=None) -> bool:
    """
    Claim + submit a BotBounty bounty. Returns True if the (live or dry-run) flow ran,
    False if there's nothing actionable. `post_json` injectable for tests. Never raises.
    """
    contact = contact or {}
    bounty_id = contact.get("bounty_id")
    if not bounty_id:
        logger.warning("BotBounty delivery: no bounty_id in contact for job_id=%s", job_id)
        return False

    live = os.getenv("BOTBOUNTY_LIVE", "").lower() in ("1", "true", "yes")
    base = os.getenv("BOTBOUNTY_API_BASE", "https://botbounty-production.up.railway.app/api").rstrip("/")
    claim_url = contact.get("claim_endpoint") or f"{base}/agent/bounties/{bounty_id}/claim"
    submit_url = contact.get("submit_endpoint") or (
        base + os.getenv("BOTBOUNTY_SUBMIT_PATH", "/agent/bounties/{id}/submit").replace("{id}", str(bounty_id))
        if not str(os.getenv("BOTBOUNTY_SUBMIT_PATH", "/agent/bounties/{id}/submit")).startswith("http")
        else os.getenv("BOTBOUNTY_SUBMIT_PATH").replace("{id}", str(bounty_id))
    )
    notes = (result_summary or "")[:50_000]

    if not live:
        logger.info("BotBounty DRY-RUN: would claim %s then submit %d chars for job %s.",
                    bounty_id, len(notes), job_id)
        return True

    api_key = os.getenv("BOTBOUNTY_API_KEY", "")
    if not api_key:
        logger.warning("BotBounty live delivery needs BOTBOUNTY_API_KEY; skipping job %s.", job_id)
        return False
    agent_id = os.getenv("BOTBOUNTY_AGENT_ID", "")
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    post = post_json or _http_post_json
    claim_body = {"agentId": agent_id} if agent_id else {}
    submit_body = {"solution": notes}
    if agent_id:
        submit_body["agentId"] = agent_id
    try:
        post(claim_url, claim_body, headers, 15.0)
        post(submit_url, submit_body, headers, 15.0)
        logger.info("BotBounty: claimed+submitted bounty %s for job %s.", bounty_id, job_id)
        return True
    except Exception as e:
        logger.warning("BotBounty delivery failed for job_id=%s bounty=%s: %s", job_id, bounty_id, e)
        return False
