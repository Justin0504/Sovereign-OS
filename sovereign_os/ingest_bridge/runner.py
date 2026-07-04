"""
Background runner: periodically fetch from all sources, dedup, normalize, buffer or POST.
"""

from __future__ import annotations

import logging
import os
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
from sovereign_os.ingest_bridge.sources.clawtasks import ClawTasksOrderSource
from sovereign_os.ingest_bridge.sources.bounty_board import taskbounty_source, stackstasker_source, botbounty_source
from sovereign_os.ingest_bridge.sources.apb import APBOrderSource

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
    if cfg.clawtasks.enabled:
        sources.append(ClawTasksOrderSource(
            base_url=cfg.clawtasks.base_url,
            min_amount_usd=cfg.clawtasks.min_amount_usd,
            max_amount_usd=cfg.clawtasks.max_amount_usd,
            tags=cfg.clawtasks.tags,
            require_funded=cfg.clawtasks.require_funded,
            limit=cfg.clawtasks.limit,
        ))
    if cfg.taskbounty.enabled:
        sources.append(taskbounty_source(
            api_key=cfg.taskbounty.api_key,
            base_url=cfg.taskbounty.base_url,
            list_path=cfg.taskbounty.list_path,
            min_amount_usd=cfg.taskbounty.min_amount_usd,
            max_amount_usd=cfg.taskbounty.max_amount_usd,
            limit=cfg.taskbounty.limit,
        ))
    if cfg.stackstasker.enabled:
        sources.append(stackstasker_source(
            base_url=cfg.stackstasker.base_url,
            list_path=cfg.stackstasker.list_path,
            min_amount_usd=cfg.stackstasker.min_amount_usd,
            max_amount_usd=cfg.stackstasker.max_amount_usd,
            limit=cfg.stackstasker.limit,
        ))
    if cfg.botbounty.enabled:
        sources.append(botbounty_source(
            base_url=cfg.botbounty.base_url,
            list_path=cfg.botbounty.list_path,
            min_amount_usd=cfg.botbounty.min_amount_usd,
            max_amount_usd=cfg.botbounty.max_amount_usd,
            limit=cfg.botbounty.limit,
        ))
    if cfg.apb.enabled and cfg.apb.publishers:
        sources.append(APBOrderSource(
            publishers=cfg.apb.publishers,
            well_known_path=cfg.apb.well_known_path,
            min_amount_usd=cfg.apb.min_amount_usd,
            max_amount_usd=cfg.apb.max_amount_usd,
            limit=cfg.apb.limit,
        ))
    return sources


def _profit_screen(raw: RawOrder) -> tuple[bool, str]:
    """
    Opt-in expected-value pre-screen (SOVEREIGN_PROFIT_SCREEN=true). Uses the CEO
    decision brain — platform-aware settlement economics + fully-loaded cost estimate
    + a success-probability prior — to drop jobs whose expected value is negative
    before they consume any compute. Off by default (returns take=True) to preserve
    existing ingest behavior.
    """
    if (os.getenv("SOVEREIGN_PROFIT_SCREEN") or "").strip().lower() not in ("1", "true", "yes", "on"):
        return True, ""
    try:
        from sovereign_os.agents.categories import category_for_skill, route_skill
        from sovereign_os.governance.opportunity import evaluate_job

        skill = route_skill("", raw.goal or "") or "summarize"
        category = category_for_skill(skill).key
        platform = raw.contact.get("platform") if isinstance(raw.contact, dict) else None
        opp = evaluate_job(raw.amount_cents, raw.goal or "", category, platform=platform)
        try:
            from sovereign_os.telemetry.tracer import record_task_screened

            record_task_screened(opp.take)
        except ImportError:
            pass
        return opp.take, opp.reason
    except Exception as e:  # noqa: BLE001 - screening must never break ingest
        logger.debug("profit screen skipped for %s: %s", raw.source_id, e)
        return True, ""


def _run_once(cfg: BridgeConfig, dedup: Deduplicator, sources: list[Any]) -> int:
    emitted = 0
    for src in sources:
        try:
            for raw in src.fetch():
                if not isinstance(raw, RawOrder):
                    continue
                if not dedup.should_emit(raw.source_id):
                    continue
                take, reason = _profit_screen(raw)
                if not take:
                    logger.info("Bridge SKIP (unprofitable) %s: %s", raw.source_id, reason)
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
