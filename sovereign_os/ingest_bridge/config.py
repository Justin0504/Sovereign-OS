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
            elif hasattr(self, key):
                if key == "port" and value is not None:
                    try:
                        value = int(value)
                    except (TypeError, ValueError):
                        pass
                setattr(self, key, value)
