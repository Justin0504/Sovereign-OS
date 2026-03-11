"""
Notification system: email, Slack, and webhook notifications on job events.

Environment variables:
  SOVEREIGN_SMTP_HOST, SOVEREIGN_SMTP_PORT, SOVEREIGN_SMTP_USER, SOVEREIGN_SMTP_PASS
  SOVEREIGN_NOTIFY_EMAIL          — recipient for job completion emails
  SOVEREIGN_SLACK_WEBHOOK_URL     — Slack incoming webhook URL
  SOVEREIGN_WEBHOOK_URL           — generic webhook (separate from per-job callback_url)
"""

from sovereign_os.notifications.dispatcher import notify_job_event

__all__ = ["notify_job_event"]
