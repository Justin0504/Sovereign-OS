"""
Bridge configuration: env vars and optional YAML overlay.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class RedditSourceConfig:
    enabled: bool = False
    client_id: str = ""
    client_secret: str = ""
    user_agent: str = "SovereignOS-Bridge/1.0"
    subreddits: list[str] = field(default_factory=lambda: ["forhire", "slavelabour", "needafavor"])
    limit_per_sub: int = 25
    min_score: int = 0
    keywords_required: list[str] = field(default_factory=list)  # empty = any


@dataclass
class ScraperSourceConfig:
    enabled: bool = False
    url: str = ""
    selector_goal: str = ""  # CSS selector or "json:path" for JSON
    selector_amount: str = ""
    selector_id: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    poll_interval_sec: int = 300


@dataclass
class RetailSourceConfig:
    enabled: bool = False
    provider: str = "shopify"  # shopify | woocommerce
    api_url: str = ""
    api_key: str = ""
    store_domain: str = ""
    order_to_goal_template: str = "Order #{order_id}: {title}"


@dataclass
class ClawTasksSourceConfig:
    enabled: bool = False
    base_url: str = "https://clawtasks.com/api"
    min_amount_usd: float = 0.0
    max_amount_usd: float = 0.0
    tags: list[str] = field(default_factory=list)
    require_funded: bool = True
    limit: int = 50


@dataclass
class TaskBountySourceConfig:
    enabled: bool = False
    api_key: str = ""
    base_url: str = "https://www.task-bounty.com/api/v1"
    list_path: str = "/tasks"
    min_amount_usd: float = 0.0
    max_amount_usd: float = 0.0
    limit: int = 50


@dataclass
class BotBountySourceConfig:
    enabled: bool = False
    base_url: str = "https://botbounty-production.up.railway.app/api"
    list_path: str = "/agent/bounties"
    min_amount_usd: float = 0.0
    max_amount_usd: float = 0.0
    limit: int = 50


@dataclass
class StacksTaskerSourceConfig:
    enabled: bool = False
    base_url: str = "https://stackstasker.com"
    list_path: str = "/tasks"
    min_amount_usd: float = 0.0
    max_amount_usd: float = 0.0
    limit: int = 50


@dataclass
class BridgeConfig:
    mode: str = "serve"  # serve | post
    host: str = "0.0.0.0"
    port: int = 9000
    sovereign_os_url: str = "http://localhost:8000"
    sovereign_os_api_key: str = ""
    poll_interval_sec: int = 60
    dedup_window_sec: int = 3600
    reddit: RedditSourceConfig = field(default_factory=RedditSourceConfig)
    scraper: ScraperSourceConfig = field(default_factory=ScraperSourceConfig)
    retail: RetailSourceConfig = field(default_factory=RetailSourceConfig)
    clawtasks: ClawTasksSourceConfig = field(default_factory=ClawTasksSourceConfig)
    taskbounty: TaskBountySourceConfig = field(default_factory=TaskBountySourceConfig)
    stackstasker: StacksTaskerSourceConfig = field(default_factory=StacksTaskerSourceConfig)
    botbounty: BotBountySourceConfig = field(default_factory=BotBountySourceConfig)

    @classmethod
    def from_env(cls, config_path: str | None = None) -> "BridgeConfig":
        config_path = config_path or os.getenv("BRIDGE_CONFIG_PATH")
        cfg = cls(
            mode=os.getenv("BRIDGE_MODE", "serve"),
            host=os.getenv("BRIDGE_HOST", "0.0.0.0"),
            port=int(os.getenv("BRIDGE_PORT", "9000")),
            sovereign_os_url=os.getenv("SOVEREIGN_OS_URL", "http://localhost:8000").rstrip("/"),
            sovereign_os_api_key=os.getenv("SOVEREIGN_OS_API_KEY", ""),
            poll_interval_sec=int(os.getenv("BRIDGE_POLL_INTERVAL_SEC", "60")),
            dedup_window_sec=int(os.getenv("BRIDGE_DEDUP_WINDOW_SEC", "3600")),
        )
        cfg.reddit = RedditSourceConfig(
            enabled=os.getenv("BRIDGE_REDDIT_ENABLED", "").lower() in ("1", "true", "yes"),
            client_id=os.getenv("REDDIT_CLIENT_ID", ""),
            client_secret=os.getenv("REDDIT_CLIENT_SECRET", ""),
            user_agent=os.getenv("REDDIT_USER_AGENT", "SovereignOS-Bridge/1.0"),
            subreddits=[s.strip() for s in os.getenv("REDDIT_SUBREDDITS", "forhire,slavelabour").split(",") if s.strip()],
            limit_per_sub=int(os.getenv("REDDIT_LIMIT_PER_SUB", "25")),
            min_score=int(os.getenv("REDDIT_MIN_SCORE", "0")),
            keywords_required=[s.strip() for s in os.getenv("REDDIT_KEYWORDS_REQUIRED", "").split(",") if s.strip()],
        )
        cfg.scraper = ScraperSourceConfig(
            enabled=os.getenv("BRIDGE_SCRAPER_ENABLED", "").lower() in ("1", "true", "yes"),
            url=os.getenv("BRIDGE_SCRAPER_URL", ""),
            selector_goal=os.getenv("BRIDGE_SCRAPER_SELECTOR_GOAL", ""),
            selector_amount=os.getenv("BRIDGE_SCRAPER_SELECTOR_AMOUNT", ""),
            selector_id=os.getenv("BRIDGE_SCRAPER_SELECTOR_ID", ""),
            poll_interval_sec=int(os.getenv("BRIDGE_SCRAPER_POLL_INTERVAL_SEC", "300")),
        )
        cfg.retail = RetailSourceConfig(
            enabled=os.getenv("BRIDGE_RETAIL_ENABLED", "").lower() in ("1", "true", "yes"),
            provider=os.getenv("BRIDGE_RETAIL_PROVIDER", "shopify"),
            api_url=os.getenv("BRIDGE_RETAIL_API_URL", ""),
            api_key=os.getenv("BRIDGE_RETAIL_API_KEY", ""),
            store_domain=os.getenv("BRIDGE_RETAIL_STORE_DOMAIN", ""),
        )
        cfg.clawtasks = ClawTasksSourceConfig(
            enabled=os.getenv("BRIDGE_CLAWTASKS_ENABLED", "").lower() in ("1", "true", "yes"),
            base_url=os.getenv("CLAWTASKS_BASE_URL", "https://clawtasks.com/api"),
            min_amount_usd=float(os.getenv("CLAWTASKS_MIN_AMOUNT_USD", "0") or 0),
            max_amount_usd=float(os.getenv("CLAWTASKS_MAX_AMOUNT_USD", "0") or 0),
            tags=[s.strip() for s in os.getenv("CLAWTASKS_TAGS", "").split(",") if s.strip()],
            require_funded=os.getenv("CLAWTASKS_REQUIRE_FUNDED", "true").lower() in ("1", "true", "yes"),
            limit=int(os.getenv("CLAWTASKS_LIMIT", "50")),
        )
        cfg.taskbounty = TaskBountySourceConfig(
            enabled=os.getenv("BRIDGE_TASKBOUNTY_ENABLED", "").lower() in ("1", "true", "yes"),
            api_key=os.getenv("TASKBOUNTY_API_KEY", ""),
            base_url=os.getenv("TASKBOUNTY_API_BASE", "https://www.task-bounty.com/api/v1"),
            list_path=os.getenv("TASKBOUNTY_LIST_PATH", "/tasks"),
            min_amount_usd=float(os.getenv("TASKBOUNTY_MIN_AMOUNT_USD", "0") or 0),
            max_amount_usd=float(os.getenv("TASKBOUNTY_MAX_AMOUNT_USD", "0") or 0),
            limit=int(os.getenv("TASKBOUNTY_LIMIT", "50")),
        )
        cfg.stackstasker = StacksTaskerSourceConfig(
            enabled=os.getenv("BRIDGE_STACKSTASKER_ENABLED", "").lower() in ("1", "true", "yes"),
            base_url=os.getenv("STACKSTASKER_API_BASE", "https://stackstasker.com"),
            list_path=os.getenv("STACKSTASKER_LIST_PATH", "/tasks"),
            min_amount_usd=float(os.getenv("STACKSTASKER_MIN_AMOUNT", "0") or 0),
            max_amount_usd=float(os.getenv("STACKSTASKER_MAX_AMOUNT", "0") or 0),
            limit=int(os.getenv("STACKSTASKER_LIMIT", "50")),
        )
        cfg.botbounty = BotBountySourceConfig(
            enabled=os.getenv("BRIDGE_BOTBOUNTY_ENABLED", "").lower() in ("1", "true", "yes"),
            base_url=os.getenv("BOTBOUNTY_API_BASE", "https://botbounty-production.up.railway.app/api"),
            list_path=os.getenv("BOTBOUNTY_LIST_PATH", "/agent/bounties"),
            min_amount_usd=float(os.getenv("BOTBOUNTY_MIN_AMOUNT_USD", "0") or 0),
            max_amount_usd=float(os.getenv("BOTBOUNTY_MAX_AMOUNT_USD", "0") or 0),
            limit=int(os.getenv("BOTBOUNTY_LIMIT", "50")),
        )
        if config_path and Path(config_path).exists():
            cfg._apply_yaml(config_path)
        return cfg

    def _apply_yaml(self, path: str) -> None:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        for key, value in data.items():
            if key == "reddit" and isinstance(value, dict):
                for k, v in value.items():
                    if hasattr(self.reddit, k):
                        setattr(self.reddit, k, v)
            elif key == "scraper" and isinstance(value, dict):
                for k, v in value.items():
                    if hasattr(self.scraper, k):
                        setattr(self.scraper, k, v)
            elif key == "retail" and isinstance(value, dict):
                for k, v in value.items():
                    if hasattr(self.retail, k):
                        setattr(self.retail, k, v)
            elif key == "clawtasks" and isinstance(value, dict):
                for k, v in value.items():
                    if hasattr(self.clawtasks, k):
                        setattr(self.clawtasks, k, v)
            elif key == "taskbounty" and isinstance(value, dict):
                for k, v in value.items():
                    if hasattr(self.taskbounty, k):
                        setattr(self.taskbounty, k, v)
            elif key == "stackstasker" and isinstance(value, dict):
                for k, v in value.items():
                    if hasattr(self.stackstasker, k):
                        setattr(self.stackstasker, k, v)
            elif key == "botbounty" and isinstance(value, dict):
                for k, v in value.items():
                    if hasattr(self.botbounty, k):
                        setattr(self.botbounty, k, v)
            elif hasattr(self, key):
                if key == "port" and value is not None:
                    try:
                        value = int(value)
                    except (TypeError, ValueError):
                        pass
                setattr(self, key, value)
