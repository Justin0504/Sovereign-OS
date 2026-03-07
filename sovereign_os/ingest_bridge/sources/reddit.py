"""
Reddit source: fetch posts from configured subreddits, parse goal and optional price.
Uses PRAW when available; else no-op. Install: pip install praw
"""

from __future__ import annotations

import logging
import re
from typing import Iterator

from sovereign_os.ingest_bridge.sources.base import RawOrder, OrderSource

logger = logging.getLogger(__name__)


def _parse_amount(text: str) -> int:
    # $5, 5 USD, 5 dollars -> 500 cents
    text = text or ""
    m = re.search(r'\$?\s*(\d+(?:\.\d+)?)\s*(?:usd|dollars?|\$)?', text, re.I)
    if m:
        try:
            return int(float(m.group(1)) * 100)
        except (ValueError, TypeError):
            pass
    return 0


class RedditOrderSource(OrderSource):
    source_name = "reddit"

    def __init__(self, client_id: str, client_secret: str, user_agent: str,
                 subreddits: list[str], limit_per_sub: int = 25, min_score: int = 0,
                 keywords_required: list[str] | None = None):
        self.client_id = client_id
        self.client_secret = client_secret
        self.user_agent = user_agent
        self.subreddits = subreddits or []
        self.limit_per_sub = limit_per_sub
        self.min_score = min_score
        self.keywords_required = keywords_required or []

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
            user_agent=self.user_agent,
        )
        seen = set()
        for sub_name in self.subreddits:
            try:
                sub = reddit.subreddit(sub_name)
                for post in sub.new(limit=self.limit_per_sub):
                    if post.score < self.min_score:
                        continue
                    if post.id in seen:
                        continue
                    seen.add(post.id)
                    title = (post.title or "").strip()
                    body = (post.selftext or "").strip()[:2000]
                    goal = f"{title}. {body}".strip() or title
                    if not goal:
                        continue
                    if self.keywords_required:
                        combined = (title + " " + body).lower()
                        if not any(kw.lower() in combined for kw in self.keywords_required):
                            continue
                    amount = _parse_amount(title) or _parse_amount(body)
                    yield RawOrder(
                        source_id=f"reddit:{post.id}",
                        goal=goal[:8000],
                        amount_cents=amount,
                        currency="USD",
                        charter="Default",
                        meta={"subreddit": sub_name, "url": f"https://reddit.com{post.permalink}"},
                    )
            except Exception as e:
                logger.exception("Reddit fetch subreddit %s: %s", sub_name, e)
        logger.info("Reddit source: yielded %s orders", len(seen))
