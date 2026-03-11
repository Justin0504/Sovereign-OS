"""
Dispatches notifications to configured channels (email, Slack, webhook).
All calls are fire-and-forget — failures are logged but never block the pipeline.
"""

from __future__ import annotations

import json
import logging
import os
import smtplib
import threading
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

logger = logging.getLogger(__name__)


def notify_job_event(
    event: str,
    job_id: int,
    goal: str,
    status: str,
    amount_cents: int = 0,
    currency: str = "USD",
    result_summary: str = "",
    audit_score: float | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Fire-and-forget notification to all configured channels."""
    threading.Thread(
        target=_dispatch,
        args=(event, job_id, goal, status, amount_cents, currency, result_summary, audit_score, extra),
        daemon=True,
    ).start()


def _dispatch(event, job_id, goal, status, amount_cents, currency, result_summary, audit_score, extra):
    ctx = {
        "event": event,
        "job_id": job_id,
        "goal": goal[:500],
        "status": status,
        "amount": f"${amount_cents / 100:.2f} {currency}" if amount_cents else "free",
        "audit_score": audit_score,
        "result_preview": (result_summary or "")[:800],
    }
    if extra:
        ctx.update(extra)

    # Email
    smtp_host = os.getenv("SOVEREIGN_SMTP_HOST")
    notify_email = os.getenv("SOVEREIGN_NOTIFY_EMAIL")
    if smtp_host and notify_email:
        try:
            _send_email(ctx, smtp_host, notify_email)
        except Exception as e:
            logger.warning("Email notification failed: %s", e)

    # Slack
    slack_url = os.getenv("SOVEREIGN_SLACK_WEBHOOK_URL")
    if slack_url:
        try:
            _send_slack(ctx, slack_url)
        except Exception as e:
            logger.warning("Slack notification failed: %s", e)

    # Generic webhook
    webhook_url = os.getenv("SOVEREIGN_WEBHOOK_URL")
    if webhook_url:
        try:
            _send_webhook(ctx, webhook_url)
        except Exception as e:
            logger.warning("Webhook notification failed: %s", e)


def _send_email(ctx: dict, smtp_host: str, to_email: str) -> None:
    smtp_port = int(os.getenv("SOVEREIGN_SMTP_PORT", "587"))
    smtp_user = os.getenv("SOVEREIGN_SMTP_USER", "")
    smtp_pass = os.getenv("SOVEREIGN_SMTP_PASS", "")

    icon = "✅" if ctx["status"] == "completed" else "❌"
    subject = f"{icon} Sovereign-OS: Job #{ctx['job_id']} {ctx['status']}"

    body = f"""Job #{ctx['job_id']} — {ctx['status'].upper()}

Goal: {ctx['goal']}
Amount: {ctx['amount']}
Audit Score: {ctx.get('audit_score', 'N/A')}

Result Preview:
{ctx.get('result_preview', '(no preview)')}

---
Sovereign-OS Notification
"""
    msg = MIMEMultipart()
    msg["From"] = smtp_user or f"sovereign-os@{smtp_host}"
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))

    with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as server:
        if smtp_port == 587:
            server.starttls()
        if smtp_user and smtp_pass:
            server.login(smtp_user, smtp_pass)
        server.sendmail(msg["From"], [to_email], msg.as_string())
    logger.info("Email notification sent to %s for job %s", to_email, ctx["job_id"])


def _send_slack(ctx: dict, url: str) -> None:
    import urllib.request

    icon = ":white_check_mark:" if ctx["status"] == "completed" else ":x:"
    text = (
        f"{icon} *Job #{ctx['job_id']}* — {ctx['status']}\n"
        f"*Goal:* {ctx['goal']}\n"
        f"*Amount:* {ctx['amount']} · *Audit:* {ctx.get('audit_score', 'N/A')}\n"
    )
    if ctx.get("result_preview"):
        text += f"```{ctx['result_preview'][:400]}```"
    payload = json.dumps({"text": text}).encode()
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
    urllib.request.urlopen(req, timeout=10)
    logger.info("Slack notification sent for job %s", ctx["job_id"])


def _send_webhook(ctx: dict, url: str) -> None:
    import urllib.request

    payload = json.dumps(ctx, default=str).encode()
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
    urllib.request.urlopen(req, timeout=10)
    logger.info("Webhook notification sent for job %s", ctx["job_id"])
