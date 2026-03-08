"""
Twitter/X source: fetch tweets mentioning work requests via search queries.
Uses tweepy when available; else no-op. Install: pip install tweepy

Required env vars:
  TWITTER_BEARER_TOKEN   — Twitter API v2 Bearer Token (read-only, free tier works)

Optional env vars:
  TWITTER_SEARCH_QUERIES — comma-separated search queries (default: writing/research/AI task requests)
  TWITTER_MAX_RESULTS    — tweets per query (default: 20, max 100 with Academic access)
"""

from __future__ import annotations

import logging
import os
import re
from typing import Iterator

from sovereign_os.ingest_bridge.sources.base import RawOrder, OrderSource

logger = logging.getLogger(__name__)

# Default search queries targeting work/service requests
DEFAULT_QUERIES = [
    "need freelance writer -is:retweet lang:en",
    "looking for content writer -is:retweet lang:en",
    "need AI writing help -is:retweet lang:en",
    "hire researcher freelance -is:retweet lang:en",
    "need blog post written -is:retweet lang:en",
    "need article written budget -is:retweet lang:en",
    "need translation help -is:retweet lang:en",
]

# Signals that indicate a genuine request (not spam or promotion)
_REQUEST_SIGNALS = [
    "need", "looking for", "hire", "help me", "want someone",
    "paying", "budget", "dm me", "DM", "lmk", "let me know",
]


def _parse_amount(text: str) -> int:
    """Extract USD cents from tweet text."""
    m = re.search(r'\$\s*(\d+(?:\.\d+)?)', text or "")
    if not m:
        m = re.search(r'(\d+(?:\.\d+)?)\s*(?:usd|dollars?)', text or "", re.I)
    if m:
        try:
            return int(float(m.group(1)) * 100)
        except (ValueError, TypeError):
            pass
    return 0


def _is_request(text: str) -> bool:
    lower = (text or "").lower()
    return any(sig.lower() in lower for sig in _REQUEST_SIGNALS)


def _clean_goal(text: str) -> str:
    """Strip URLs and hashtags from tweet text to form a clean goal."""
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"#\w+", "", text)
    text = re.sub(r"@\w+", "", text)
    return " ".join(text.split()).strip()


class TwitterOrderSource(OrderSource):
    source_name = "twitter"

    def __init__(
        self,
        bearer_token: str | None = None,
        queries: list[str] | None = None,
        max_results: int = 20,
    ):
        self.bearer_token = bearer_token or os.getenv("TWITTER_BEARER_TOKEN", "")
        raw_queries = os.getenv("TWITTER_SEARCH_QUERIES", "")
        if raw_queries:
            self.queries = [q.strip() for q in raw_queries.split(",") if q.strip()]
        else:
            self.queries = queries or DEFAULT_QUERIES
        self.max_results = min(max(1, max_results), 100)

    def fetch(self) -> Iterator[RawOrder]:
        if not self.bearer_token:
            logger.warning(
                "Twitter source: set TWITTER_BEARER_TOKEN to enable "
                "(get one free at developer.twitter.com)"
            )
            return
        try:
            import tweepy
        except ImportError:
            logger.warning("Twitter source: install 'tweepy' to enable (pip install tweepy)")
            return

        client = tweepy.Client(bearer_token=self.bearer_token, wait_on_rate_limit=True)
        seen: set[str] = set()
        total_emitted = 0

        for query in self.queries:
            try:
                # Search recent tweets (last 7 days, free tier)
                response = client.search_recent_tweets(
                    query=query,
                    max_results=min(self.max_results, 100),
                    tweet_fields=["author_id", "created_at", "text", "public_metrics"],
                    expansions=["author_id"],
                    user_fields=["username"],
                )
                if not response.data:
                    continue

                # Build author_id -> username map
                users: dict[str, str] = {}
                if response.includes and response.includes.get("users"):
                    for u in response.includes["users"]:
                        users[str(u.id)] = u.username

                for tweet in response.data:
                    tid = str(tweet.id)
                    if tid in seen:
                        continue
                    seen.add(tid)

                    text = (tweet.text or "").strip()
                    if not text or not _is_request(text):
                        continue

                    goal = _clean_goal(text)
                    if not goal or len(goal) < 20:
                        continue

                    amount = _parse_amount(text)
                    if amount == 0:
                        amount = 800  # $8 default floor for Twitter requests

                    author_id = str(tweet.author_id) if tweet.author_id else ""
                    username = users.get(author_id, "")
                    tweet_url = f"https://twitter.com/i/web/status/{tid}"

                    contact = {
                        "platform": "twitter",
                        "username": username or author_id,
                        "tweet_id": tid,
                        "tweet_url": tweet_url,
                    } if username or author_id else None

                    yield RawOrder(
                        source_id=f"twitter:{tid}",
                        goal=goal[:8000],
                        amount_cents=amount,
                        currency="USD",
                        charter="Default",
                        meta={
                            "tweet_url": tweet_url,
                            "query": query,
                            "likes": (tweet.public_metrics or {}).get("like_count", 0),
                        },
                        contact=contact,
                    )
                    total_emitted += 1

            except Exception as e:
                logger.warning("Twitter fetch query '%s': %s", query[:60], e)

        logger.info("Twitter source: emitted %d orders", total_emitted)
