"""
Reddit source: fetch posts from configured subreddits, parse goal and optional price.
Uses PRAW when available; else no-op. Install: pip install praw

Default subreddits for freelance/content work:
  r/forhire, r/slavelabour, r/HireaWriter, r/jobs4bitcoins, r/WorkOnline
"""

from __future__ import annotations

import logging
import re
from typing import Iterator

from sovereign_os.ingest_bridge.sources.base import RawOrder, OrderSource

logger = logging.getLogger(__name__)

# Work-intent keywords — post must contain at least one to be considered
_WORK_KEYWORDS = [
    "looking for", "need someone", "need a", "need help", "hire",
    "freelance", "help me", "write", "research", "translate", "summarize",
    "create", "draft", "review", "analyze", "build", "design",
    "paying", "budget", "usd", "$", "offer",
]

# Subreddits to skip (entertainment, memes, etc.)
_SKIP_SUBREDDITS: set[str] = {"memes", "funny", "gaming", "pics", "videos"}

# Default target subreddits for content/AI work
DEFAULT_SUBREDDITS = [
    "forhire", "slavelabour", "HireaWriter", "jobs4bitcoins", "WorkOnline",
]


def _parse_amount(text: str) -> int:
    """Extract USD cents from text: '$5', '5 USD', '5 dollars' → 500."""
    text = text or ""
    m = re.search(r'\$\s*(\d+(?:\.\d+)?)', text, re.I)
    if not m:
        m = re.search(r'(\d+(?:\.\d+)?)\s*(?:usd|dollars?)', text, re.I)
    if m:
        try:
            return int(float(m.group(1)) * 100)
        except (ValueError, TypeError):
            pass
    return 0


def _clean_goal(title: str, body: str) -> str:
    """Build a concise goal from post title + first relevant paragraph of body."""
    title = (title or "").strip()
    body = (body or "").strip()
    # Take first 600 chars of body, stopping at first double newline
    short_body = body.split("\n\n")[0][:600].strip()
    if short_body and short_body.lower() != title.lower():
        return f"{title}. {short_body}"
    return title


def _is_work_request(title: str, body: str) -> bool:
    """Return True if post appears to be a legitimate work request."""
    combined = (title + " " + body).lower()
    return any(kw in combined for kw in _WORK_KEYWORDS)


class RedditOrderSource(OrderSource):
    source_name = "reddit"

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        user_agent: str,
        subreddits: list[str] | None = None,
        limit_per_sub: int = 30,
        min_score: int = 0,
        keywords_required: list[str] | None = None,
        require_work_intent: bool = True,
    ):
        self.client_id = client_id
        self.client_secret = client_secret
        self.user_agent = user_agent
        self.subreddits = subreddits or DEFAULT_SUBREDDITS
        self.limit_per_sub = limit_per_sub
        self.min_score = min_score
        self.keywords_required = keywords_required or []
        self.require_work_intent = require_work_intent

    def fetch(self) -> Iterator[RawOrder]:
        try:
            import praw
        except ImportError:
            logger.warning("Reddit source: install 'praw' to enable (pip install praw)")
            return
        if not self.client_id or not self.client_secret:
            logger.warning("Reddit source: REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET required")
            return
        reddit = praw.Reddit(
            client_id=self.client_id,
            client_secret=self.client_secret,
            user_agent=self.user_agent or "sovereign-os-ingest/1.0",
        )
        seen: set[str] = set()
        total_emitted = 0
        for sub_name in self.subreddits:
            if sub_name.lower() in _SKIP_SUBREDDITS:
                continue
            try:
                sub = reddit.subreddit(sub_name)
                for post in sub.new(limit=self.limit_per_sub):
                    if post.score < self.min_score:
                        continue
                    if post.id in seen:
                        continue
                    seen.add(post.id)
                    title = (post.title or "").strip()
                    body = (post.selftext or "").strip()[:3000]
                    if not title:
                        continue
                    # Optional keyword gate
                    if self.keywords_required:
                        combined = (title + " " + body).lower()
                        if not any(kw.lower() in combined for kw in self.keywords_required):
                            continue
                    # Work-intent filter
                    if self.require_work_intent and not _is_work_request(title, body):
                        continue
                    goal = _clean_goal(title, body)
                    amount = _parse_amount(title) or _parse_amount(body)
                    # Default minimum amount for requests without explicit pricing
                    if amount == 0:
                        amount = 500  # $5 floor
                    author_name = (getattr(post.author, "name", None) or "").strip() or None
                    permalink = getattr(post, "permalink", "") or f"/r/{sub_name}/comments/{post.id}"
                    contact = {
                        "platform": "reddit",
                        "username": author_name or "anonymous",
                        "post_id": post.id,
                        "permalink": permalink,
                        "post_url": f"https://reddit.com{permalink}",
                    } if author_name else None
                    yield RawOrder(
                        source_id=f"reddit:{post.id}",
                        goal=goal[:8000],
                        amount_cents=amount,
                        currency="USD",
                        charter="Default",
                        meta={
                            "subreddit": sub_name,
                            "post_url": f"https://reddit.com{permalink}",
                            "post_score": post.score,
                            "flair": getattr(post, "link_flair_text", "") or "",
                        },
                        contact=contact,
                    )
                    total_emitted += 1
            except Exception as e:
                logger.exception("Reddit fetch subreddit %s: %s", sub_name, e)
        logger.info("Reddit source: emitted %d orders from %d subreddits", total_emitted, len(self.subreddits))
