"""
send_email connector — the concrete implementation behind the `send_email`
connector spec, used by the email delivery category to actually send a drafted
email. Reuses the SMTP configuration of notifications/dispatcher.

Money/reputation-bearing (it sends real mail), so it is DRY-RUN by default and
only sends when live=True (env SOVEREIGN_EMAIL_LIVE=true). In dry-run it logs the
intended message and returns {"sent": False, "dry_run": True}.

Env: SOVEREIGN_SMTP_HOST, SOVEREIGN_SMTP_PORT (587), SOVEREIGN_SMTP_USER,
SOVEREIGN_SMTP_PASS.
"""

from __future__ import annotations

import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

logger = logging.getLogger(__name__)


def is_configured() -> bool:
    """True when SMTP is configured enough to send (matches the registry spec env keys)."""
    return bool(os.getenv("SOVEREIGN_SMTP_HOST") and os.getenv("SOVEREIGN_SMTP_USER"))


def send_email(
    to: str,
    subject: str,
    body: str,
    *,
    live: bool | None = None,
    smtp: smtplib.SMTP | None = None,
) -> dict:
    """
    Send an email. Dry-run unless live=True (or SOVEREIGN_EMAIL_LIVE truthy).

    `smtp` lets callers/tests inject a connection; otherwise one is opened from env.
    Returns {"sent": bool, "dry_run": bool, "to": str, "error": str?}.
    """
    live = (os.getenv("SOVEREIGN_EMAIL_LIVE", "").lower() in ("1", "true", "yes")) if live is None else live
    to = (to or "").strip()
    if not to:
        return {"sent": False, "error": "no recipient"}

    if not live:
        logger.info("CONNECTOR send_email DRY-RUN: would send to %s (subj=%r, %d chars).", to, subject[:60], len(body or ""))
        return {"sent": False, "dry_run": True, "to": to}

    host = os.getenv("SOVEREIGN_SMTP_HOST")
    if not host and smtp is None:
        return {"sent": False, "error": "SMTP not configured (SOVEREIGN_SMTP_HOST)"}

    port = int(os.getenv("SOVEREIGN_SMTP_PORT", "587"))
    user = os.getenv("SOVEREIGN_SMTP_USER", "")
    password = os.getenv("SOVEREIGN_SMTP_PASS", "")
    sender = user or f"sovereign-os@{host}"

    msg = MIMEMultipart()
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = subject or "(no subject)"
    msg.attach(MIMEText(body or "", "plain", "utf-8"))

    def _send(server: smtplib.SMTP) -> None:
        if smtp is None and port == 587:
            server.starttls()
        if smtp is None and user and password:
            server.login(user, password)
        server.sendmail(sender, [to], msg.as_string())

    try:
        if smtp is not None:
            _send(smtp)
        else:
            with smtplib.SMTP(host, port, timeout=15) as server:
                _send(server)
        logger.info("CONNECTOR send_email: sent to %s.", to)
        return {"sent": True, "to": to}
    except Exception as e:
        logger.warning("CONNECTOR send_email failed: %s", e)
        return {"sent": False, "error": str(e), "to": to}
