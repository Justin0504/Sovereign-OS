"""
Reddit delivery: post job result as a comment (or DM) so the client is contacted after completion.

Requires PRAW with write scope. Set REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET, and either
REDDIT_REFRESH_TOKEN (OAuth2) or REDDIT_USERNAME + REDDIT_PASSWORD (script app) to enable.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


def deliver_result_to_reddit(contact: dict[str, Any], result_summary: str, job_id: int) -> bool:
    """
    Post the job result as a comment on the Reddit post so the OP is notified.
    contact must have: platform="reddit", post_id, permalink (optional).
    Returns True if comment was posted, False if skipped or failed.
    """
    if not contact or contact.get("platform") != "reddit":
        return False
    post_id = contact.get("post_id")
    if not post_id:
        logger.warning("Reddit delivery: missing post_id in contact")
        return False
    try:
        import praw
    except ImportError:
        logger.warning("Reddit delivery: install 'praw' to enable (pip install praw)")
        return False
    client_id = os.getenv("REDDIT_CLIENT_ID", "").strip()
    client_secret = os.getenv("REDDIT_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        logger.debug("Reddit delivery: REDDIT_CLIENT_ID/SECRET not set; skip")
        return False
    user_agent = os.getenv("REDDIT_USER_AGENT", "Sovereign-OS/1.0 job delivery").strip()
    # Prefer refresh token (OAuth2); fallback to script (username/password)
    refresh_token = os.getenv("REDDIT_REFRESH_TOKEN", "").strip()
    username = os.getenv("REDDIT_USERNAME", "").strip()
    password = os.getenv("REDDIT_PASSWORD", "").strip()
    if not refresh_token and not (username and password):
        logger.warning("Reddit delivery: set REDDIT_REFRESH_TOKEN or REDDIT_USERNAME+REDDIT_PASSWORD to post comments")
        return False
    try:
        if refresh_token:
            reddit = praw.Reddit(
                client_id=client_id,
                client_secret=client_secret,
                refresh_token=refresh_token,
                user_agent=user_agent,
            )
        else:
            reddit = praw.Reddit(
                client_id=client_id,
                client_secret=client_secret,
                username=username,
                password=password,
                user_agent=user_agent,
            )
        submission = reddit.submission(id=post_id)
        # Keep comment under Reddit limit; prefix so OP knows it's the delivery
        max_len = 9500
        body = result_summary[:max_len] + ("…" if len(result_summary) > max_len else "")
        comment_text = f"**[Delivery for job #{job_id}]**\n\n{body}"
        submission.reply(comment_text)
        logger.info("Reddit delivery: posted comment on post_id=%s for job_id=%s", post_id, job_id)
        return True
    except Exception as e:
        logger.warning("Reddit delivery failed for job_id=%s: %s", job_id, e)
        return False
