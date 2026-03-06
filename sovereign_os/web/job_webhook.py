"""
Job completion webhook: POST to SOVEREIGN_WEBHOOK_URL or per-job callback_url
when a job reaches completed or payment_failed. Retries with backoff; optional HMAC signature.
On final failure, appends payload and error to SOVEREIGN_WEBHOOK_LOG_PATH (default data/webhook_log.jsonl).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
import urllib.request
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_WEBHOOK_LOG_PATH = "data/webhook_log.jsonl"

RESULT_SUMMARY_MAX_BYTES = 2048
WEBHOOK_RETRIES = 3
WEBHOOK_BACKOFF_SEC = [1, 2, 4]


def _build_payload(
    job_id: int,
    status: str,
    goal: str,
    amount_cents: int,
    currency: str,
    payment_id: str | None,
    completed_at: str,
    result_summary: str,
    audit_score: float,
    charter: str,
    request_id: str | None = None,
) -> dict[str, Any]:
    out = {
        "job_id": job_id,
        "status": status,
        "goal": goal,
        "amount_cents": amount_cents,
        "currency": currency,
        "payment_id": payment_id,
        "completed_at": completed_at,
        "result_summary": (result_summary or "")[:RESULT_SUMMARY_MAX_BYTES],
        "audit_score": audit_score,
        "charter": charter,
    }
    if request_id:
        out["request_id"] = request_id
    return out


def _sign_payload(body_bytes: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body_bytes, hashlib.sha256).hexdigest()


def _write_failure_log(
    url: str,
    payload: dict[str, Any],
    error: str,
    log_path: str | Path | None = None,
) -> None:
    """Append one JSONL line to log_path for failed webhook delivery (post-retries)."""
    path = Path(log_path or os.getenv("SOVEREIGN_WEBHOOK_LOG_PATH", DEFAULT_WEBHOOK_LOG_PATH))
    if not path:
        return
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(
        {
            "url": url,
            "job_id": payload.get("job_id"),
            "status": payload.get("status"),
            "error": error,
            "ts": time.time(),
        },
        ensure_ascii=False,
    ) + "\n"
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
        logger.info("Webhook failure logged to %s for job_id=%s", path, payload.get("job_id"))
    except OSError as e:
        logger.warning("Could not write webhook failure log to %s: %s", path, e)


def _post(url: str, payload: dict[str, Any], secret: str | None) -> bool:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if secret:
        headers["X-Sovereign-Signature"] = _sign_payload(body, secret)
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            if 200 <= resp.getcode() < 300:
                return True
            logger.warning("Webhook %s returned %s", url, resp.getcode())
            return False
    except Exception as e:
        logger.warning("Webhook POST to %s failed: %s", url, e)
        raise


def notify_job_completion(
    webhook_url: str,
    job_id: int,
    status: str,
    goal: str,
    amount_cents: int,
    currency: str,
    payment_id: str | None,
    completed_at: str,
    result_summary: str,
    audit_score: float,
    charter: str,
    *,
    secret: str | None = None,
    request_id: str | None = None,
) -> None:
    """
    POST job completion payload to webhook_url. Retries up to WEBHOOK_RETRIES with backoff.
    If secret is set, adds X-Sovereign-Signature (HMAC-SHA256 of body).
    request_id is included in payload when set for traceability.
    """
    payload = _build_payload(
        job_id=job_id,
        status=status,
        goal=goal,
        amount_cents=amount_cents,
        currency=currency,
        payment_id=payment_id,
        completed_at=completed_at,
        result_summary=result_summary,
        audit_score=audit_score,
        charter=charter,
        request_id=request_id,
    )
    last_err: Exception | None = None
    for i, delay in enumerate(WEBHOOK_BACKOFF_SEC):
        try:
            _post(webhook_url, payload, secret)
            logger.info("Webhook delivered to %s for job_id=%s status=%s", webhook_url, job_id, status)
            return
        except Exception as e:
            last_err = e
            if i < WEBHOOK_RETRIES - 1:
                time.sleep(delay)
    err_msg = str(last_err) if last_err else "unknown"
    logger.exception("Webhook failed after %s retries for job_id=%s: %s", WEBHOOK_RETRIES, job_id, last_err)
    log_path = os.getenv("SOVEREIGN_WEBHOOK_LOG_PATH", DEFAULT_WEBHOOK_LOG_PATH)
    if log_path:
        _write_failure_log(webhook_url, payload, err_msg, log_path)
