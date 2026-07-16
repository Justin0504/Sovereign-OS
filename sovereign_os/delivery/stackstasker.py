"""
Deliver a completed job back to StacksTasker. Its worker flow is bid-based
(open → bidding → in-progress → submitted → completed), so delivery = bid on the
task to claim it, then submit the solution.

Bidding stakes STX on Stacks (testnet) and is money-moving, so DRY-RUN unless
STACKSTASKER_LIVE=true. The bid endpoint is `POST /tasks/{id}/bid`; the submit
path isn't publicly documented, so it's configurable (STACKSTASKER_SUBMIT_PATH,
default "/tasks/{id}/submit"). `post_json` is injectable for tests.

Env: STACKSTASKER_LIVE, STACKSTASKER_AGENT_ID, STACKSTASKER_API_BASE,
STACKSTASKER_SUBMIT_PATH.
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


def deliver_result_to_stackstasker(contact: dict, result_summary: str, job_id: str, *, post_json=None) -> bool:
    """
    Bid on the task then submit the solution. Returns True if the (live or dry-run)
    flow ran. Never raises into the worker.
    """
    task_id = (contact or {}).get("bounty_id")
    if not task_id:
        logger.warning("StacksTasker delivery: no bounty_id in contact for job_id=%s", job_id)
        return False

    live = os.getenv("STACKSTASKER_LIVE", "").lower() in ("1", "true", "yes")
    if not live:
        logger.info("StacksTasker DRY-RUN: would bid+submit task %s for job %s.", task_id, job_id)
        return True

    agent_id = os.getenv("STACKSTASKER_AGENT_ID", "")
    if not agent_id:
        logger.warning("StacksTasker live delivery needs STACKSTASKER_AGENT_ID; skipping job %s.", job_id)
        return False

    base = os.getenv("STACKSTASKER_API_BASE", "https://stackstasker.com").rstrip("/")
    submit_path = os.getenv("STACKSTASKER_SUBMIT_PATH", "/tasks/{id}/submit").replace("{id}", str(task_id))
    post = post_json or _http_post_json
    headers = {"Content-Type": "application/json"}
    notes = (result_summary or "")[:5000]
    bid_body = {"agentId": agent_id, "message": "Completed via Sovereign-OS", "currency": "STX"}
    # Dynamic thin-margin bid: when the contact carries the reward ceiling and our cost
    # estimate, bid the lowest profitable price to win on volume. Absent that data the
    # bid is unpriced (unchanged behavior).
    ceiling = (contact or {}).get("reward_cents")
    est_cost = (contact or {}).get("est_cost_cents")
    if ceiling and est_cost:
        try:
            from sovereign_os.governance.bidding import recommended_bid_cents

            bid = recommended_bid_cents(int(est_cost), int(ceiling))
            if bid is None:
                logger.info("StacksTasker: task %s unprofitable at ceiling %s; skipping bid.", task_id, ceiling)
                return False
            bid_body["bidAmount"] = bid
        except Exception as e:  # noqa: BLE001 - pricing must not break delivery
            logger.debug("StacksTasker bid pricing skipped: %s", e)
    try:
        post(f"{base}/tasks/{task_id}/bid?currency=STX", bid_body, headers, 15.0)
        post(f"{base}{('/' + submit_path.lstrip('/'))}",
             {"agentId": agent_id, "result": notes}, headers, 15.0)
        logger.info("StacksTasker: bid+submitted task %s for job %s.", task_id, job_id)
        return True
    except Exception as e:
        logger.warning("StacksTasker delivery failed for job_id=%s task=%s: %s", job_id, task_id, e)
        return False
