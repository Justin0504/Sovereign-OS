"""
Order sources: Reddit, Twitter/X, generic scraper, retail APIs.
"""

from sovereign_os.ingest_bridge.sources.base import RawOrder, OrderSource
from sovereign_os.ingest_bridge.sources.reddit import RedditOrderSource
from sovereign_os.ingest_bridge.sources.twitter import TwitterOrderSource
from sovereign_os.ingest_bridge.sources.scraper import ScraperOrderSource
from sovereign_os.ingest_bridge.sources.clawtasks import ClawTasksOrderSource, ClawTasksClient
from sovereign_os.ingest_bridge.sources.bounty_board import (
    BountyFieldMap,
    GenericBountySource,
    botbounty_source,
    stackstasker_source,
    taskbounty_source,
)

__all__ = [
    "RawOrder",
    "OrderSource",
    "RedditOrderSource",
    "TwitterOrderSource",
    "ScraperOrderSource",
    "ClawTasksOrderSource",
    "ClawTasksClient",
    "BountyFieldMap",
    "GenericBountySource",
    "taskbounty_source",
    "stackstasker_source",
    "botbounty_source",
]
