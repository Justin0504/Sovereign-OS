"""
Deliver a completed job's result back to an APB (Agent Payment Bounty) publisher —
the last mile of the x402 discovery→delivery→reward loop.

An APB bounty (discovered via /.well-known/bounties.json) carries a `claim` field
describing how to submit the completed work. That field is not yet standardized, so
this adapter is tolerant:

  - `claim` is a URL string           -> POST the result there.
  - `claim` is an object with a URL    -> POST there (url|endpoint|submitUrl|claimUrl).
  - `claim` is prose/steps (no URL)    -> can't auto-submit; log and report not-delivered
                                          (a human or a publisher-specific flow must claim).

Submitting a deliverable is reputation/reward-bearing, so it runs in DRY-RUN unless
APB_LIVE=true — in dry-run it logs the intended POST and moves nothing. The reward
itself settles over x402/USDC to the bounty's payTo when the publisher verifies the
work; this adapter does not move funds, it only submits the result to claim.

Env:
  APB_LIVE      "true" to actually POST the claim submission
  APB_API_KEY   optional bearer token for the publisher's claim endpoint
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


def _http_post_json(url: str, body: dict, headers: dict, timeout: float) -> Any:
    import requests  # type: ignore[import]

    r = requests.post(url, json=body, headers=headers, timeout=timeout)
    r.raise_for_status()
    return r.json()


def _claim_url(claim: Any) -> str:
    """Extract a submit URL from the bounty's `claim` field, or '' if there isn't one."""
    if isinstance(claim, str) and claim.strip().lower().startswith(("http://", "https://")):
        return claim.strip()
    if isinstance(claim, dict):
        for key in ("submitUrl", "submit_url", "url", "endpoint", "claimUrl", "claim_url"):
            v = claim.get(key)
            if isinstance(v, str) and v.strip().lower().startswith(("http://", "https://")):
                return v.strip()
    return ""


def deliver_result_to_apb(contact: dict, result_summary: str, job_id: str, *, post_json=None) -> bool:
    """
    Submit the job's result to an APB bounty's claim endpoint. Returns True if the
    (live or dry-run) flow ran, False if there's nothing actionable (no bounty_id, or
    no submit URL when live). `post_json` is injectable for tests. Never raises.
    """
    contact = contact or {}
    bounty_id = contact.get("bounty_id")
    if not bounty_id:
        logger.warning("APB delivery: no bounty_id in contact for job_id=%s", job_id)
        return False

    claim = contact.get("claim")
    url = _claim_url(claim)
    pay_to = contact.get("pay_to") or ""
    network = contact.get("network") or "base"
    asset = contact.get("asset") or "USDC"
    live = os.getenv("APB_LIVE", "").lower() in ("1", "true", "yes")
    notes = (result_summary or "")[:50_000]

    if not live:
        logger.info(
            "APB DRY-RUN: would submit bounty %s (job %s) to %s; reward settles via x402 "
            "%s/%s to %s.", bounty_id, job_id, url or "(no claim URL — steps only)",
            asset, network, pay_to or "(payTo unset)",
        )
        return True

    if not url:
        logger.warning(
            "APB live delivery: bounty %s has no claim URL (claim=%r); cannot auto-submit job %s.",
            bounty_id, claim, job_id,
        )
        return False

    body = {"bountyId": str(bounty_id), "result": notes}
    if pay_to:
        body["payTo"] = pay_to
    headers = {"Content-Type": "application/json"}
    api_key = os.getenv("APB_API_KEY", "")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        (post_json or _http_post_json)(url, body, headers, 15.0)
        logger.info("APB: submitted bounty %s for job %s (reward via x402 %s/%s to %s).",
                    bounty_id, job_id, asset, network, pay_to or "(payTo unset)")
        return True
    except Exception as e:
        logger.warning("APB delivery failed for job_id=%s bounty=%s: %s", job_id, bounty_id, e)
        return False
