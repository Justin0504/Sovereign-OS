"""
Background runner: periodically fetch from all sources, dedup, normalize, buffer or POST.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from sovereign_os.ingest_bridge.config import BridgeConfig
from sovereign_os.ingest_bridge.dedup import Deduplicator
from sovereign_os.ingest_bridge.normalizer import to_job_payload
from sovereign_os.ingest_bridge.output import buffer_append, post_to_sovereign
from sovereign_os.ingest_bridge.sources.base import RawOrder
from sovereign_os.ingest_bridge.sources.reddit import RedditOrderSource
from sovereign_os.ingest_bridge.sources.scraper import ScraperOrderSource
from sovereign_os.ingest_bridge.sources.retail import RetailOrderSource

logger = logging.getLogger(__name__)

_stop = threading.Event()


def _sources_from_config(cfg: BridgeConfig) -> list[Any]:
    sources = []
    if cfg.reddit.enabled and cfg.reddit.client_id and cfg.reddit.client_secret:
        sources.append(RedditOrderSource(
            client_id=cfg.reddit.client_id,
            client_secret=cfg.reddit.client_secret,
            user_agent=cfg.reddit.user_agent,
            subreddits=cfg.reddit.subreddits,
            limit_per_sub=cfg.reddit.limit_per_sub,
            min_score=cfg.reddit.min_score,
            keywords_required=cfg.reddit.keywords_required,
        ))
    if cfg.scraper.enabled and cfg.scraper.url:
        sources.append(ScraperOrderSource(
            url=cfg.scraper.url,
            selector_goal=cfg.scraper.selector_goal,
            selector_amount=cfg.scraper.selector_amount,
            selector_id=cfg.scraper.selector_id,
            headers=cfg.scraper.headers,
        ))
    if cfg.retail.enabled and cfg.retail.api_url and cfg.retail.api_key:
        sources.append(RetailOrderSource(
            provider=cfg.retail.provider,
            api_url=cfg.retail.api_url,
            api_key=cfg.retail.api_key,
            store_domain=cfg.retail.store_domain,
            order_to_goal_template=cfg.retail.order_to_goal_template,
        ))
    return sources


def _run_once(cfg: BridgeConfig, dedup: Deduplicator, sources: list[Any]) -> int:
    emitted = 0
    for src in sources:
        try:
            for raw in src.fetch():
                if not isinstance(raw, RawOrder):
                    continue
                if not dedup.should_emit(raw.source_id):
                    continue
                payload = to_job_payload(raw)
                if cfg.mode == "serve":
                    buffer_append(payload)
                    emitted += 1
                else:
                    if post_to_sovereign(cfg.sovereign_os_url, cfg.sovereign_os_api_key, payload):
                        emitted += 1
        except Exception as e:
            logger.exception("Source %s: %s", getattr(src, "source_name", "?"), e)
    return emitted


def start_runner(cfg: BridgeConfig) -> threading.Thread:
    dedup = Deduplicator(window_sec=cfg.dedup_window_sec)
    sources = _sources_from_config(cfg)
    if not sources:
        logger.warning("No sources enabled; set BRIDGE_REDDIT_*, BRIDGE_SCRAPER_*, or BRIDGE_RETAIL_*")

    def _loop():
        while not _stop.is_set():
            try:
                n = _run_once(cfg, dedup, sources)
                if n:
                    logger.info("Bridge emitted %s job(s)", n)
            except Exception as e:
                logger.exception("Bridge run: %s", e)
            _stop.wait(cfg.poll_interval_sec)

    t = threading.Thread(target=_loop, daemon=True)
    t.start()
    logger.info("Bridge runner started (poll_interval=%ss, mode=%s)", cfg.poll_interval_sec, cfg.mode)
    return t


def stop_runner() -> None:
    _stop.set()
