"""
Deliver a completed coding job back to TaskBounty: submit the PR (and notes) as
the bounty solution. Closes the inbound coding loop — ingest a bug-fix bounty →
govern → agent fixes it and opens a PR (submit_pr connector) → this reports the PR
to TaskBounty.

Money/reputation-bearing (it submits a paid deliverable), so DRY-RUN unless
TASKBOUNTY_LIVE=true. The exact submit path isn't publicly documented, so it's
configurable (TASKBOUNTY_SUBMIT_PATH, default "/tasks/{id}/submit"); align it to
the account's API when going live.

Env: TASKBOUNTY_API_KEY (tb_live_*), TASKBOUNTY_LIVE, TASKBOUNTY_API_BASE,
TASKBOUNTY_SUBMIT_PATH.
"""

from __future__ import annotations

import logging
import os
import re

logger = logging.getLogger(__name__)

_PR_URL = re.compile(r"https?://(?:www\.)?github\.com/[\w.-]+/[\w.-]+/pull/\d+")


def extract_pr_url(text: str) -> str:
    """Find a GitHub PR link in the deliverable (the coding worker's submit_pr output)."""
    m = _PR_URL.search(text or "")
    return m.group(0) if m else ""


def _http_post_json(url: str, body: dict, headers: dict, timeout: float):
    import requests  # type: ignore[import]

    r = requests.post(url, json=body, headers=headers, timeout=timeout)
    r.raise_for_status()
    return r.json()


def deliver_result_to_taskbounty(contact: dict, result_summary: str, job_id: str, *, post_json=None) -> bool:
    """
    Submit the job's PR/notes to TaskBounty. Returns True if the (live or dry-run)
    flow ran. `post_json` is injectable for tests. Never raises into the worker.
    """
    task_id = (contact or {}).get("bounty_id")
    if not task_id:
        logger.warning("TaskBounty delivery: no bounty_id in contact for job_id=%s", job_id)
        return False

    live = os.getenv("TASKBOUNTY_LIVE", "").lower() in ("1", "true", "yes")
    pr_url = extract_pr_url(result_summary)
    notes = (result_summary or "")[:5000]

    if not live:
        logger.info("TaskBounty DRY-RUN: would submit task %s (pr=%s) for job %s.", task_id, pr_url or "(none)", job_id)
        return True

    api_key = os.getenv("TASKBOUNTY_API_KEY", "")
    if not api_key:
        logger.warning("TaskBounty live submit needs TASKBOUNTY_API_KEY; skipping job %s.", job_id)
        return False
    base = os.getenv("TASKBOUNTY_API_BASE", "https://www.task-bounty.com/api/v1").rstrip("/")
    path = os.getenv("TASKBOUNTY_SUBMIT_PATH", "/tasks/{id}/submit").replace("{id}", str(task_id))
    url = base + ("/" + path.lstrip("/"))
    body = {"prUrl": pr_url, "notes": notes}
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    try:
        (post_json or _http_post_json)(url, body, headers, 15.0)
        logger.info("TaskBounty: submitted task %s (pr=%s) for job %s.", task_id, pr_url, job_id)
        return True
    except Exception as e:
        logger.warning("TaskBounty submit failed for job_id=%s task=%s: %s", job_id, task_id, e)
        return False
