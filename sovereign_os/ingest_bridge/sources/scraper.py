"""
Generic scraper source: fetch a URL, extract goal/amount via CSS selectors or JSON path.
Uses requests + BeautifulSoup when available. Install: pip install requests beautifulsoup4
"""

from __future__ import annotations

import logging
import re
from typing import Iterator

from sovereign_os.ingest_bridge.sources.base import RawOrder, OrderSource

logger = logging.getLogger(__name__)


def _text(el) -> str:
    if el is None:
        return ""
    return (getattr(el, "get_text", lambda: str(el))() or str(el)).strip()


class ScraperOrderSource(OrderSource):
    source_name = "scraper"

    def __init__(self, url: str, selector_goal: str = "", selector_amount: str = "",
                 selector_id: str = "", headers: dict | None = None):
        self.url = url
        self.selector_goal = selector_goal
        self.selector_amount = selector_amount
        self.selector_id = selector_id
        self.headers = headers or {}

    def fetch(self) -> Iterator[RawOrder]:
        if not self.url:
            logger.warning("Scraper source: BRIDGE_SCRAPER_URL required")
            return
        try:
            import requests
        except ImportError:
            logger.warning("Scraper source: install 'requests' (pip install requests)")
            return
        try:
            r = requests.get(self.url, headers=self.headers, timeout=30)
            r.raise_for_status()
            content_type = (r.headers.get("Content-Type") or "").lower()
            if "json" in content_type:
                yield from self._parse_json(r.json())
            else:
                yield from self._parse_html(r.text)
        except Exception as e:
            logger.exception("Scraper fetch %s: %s", self.url, e)

    def _parse_json(self, data: dict | list) -> Iterator[RawOrder]:
        if isinstance(data, list):
            for i, item in enumerate(data):
                if isinstance(item, dict):
                    goal = item.get("goal") or item.get("title") or item.get("description") or str(item)[:500]
                    amount = int(item.get("amount_cents", 0) or 0)
                    if not amount and "amount" in item:
                        try:
                            amount = int(float(item["amount"]) * 100)
                        except (TypeError, ValueError):
                            pass
                    sid = item.get("id") or item.get("source_id") or str(i)
                    yield RawOrder(
                        source_id=f"scraper:{sid}",
                        goal=str(goal)[:8000],
                        amount_cents=amount,
                        currency=str(item.get("currency", "USD")),
                        charter="Default",
                        meta=item,
                    )
        elif isinstance(data, dict) and "jobs" in data:
            yield from self._parse_json(data["jobs"])
        elif isinstance(data, dict):
            goal = data.get("goal") or data.get("title") or str(data)[:500]
            amount = int(data.get("amount_cents", 0) or 0)
            yield RawOrder(
                source_id=f"scraper:{data.get('id', '0')}",
                goal=str(goal)[:8000],
                amount_cents=amount,
                currency=str(data.get("currency", "USD")),
                charter="Default",
                meta=data,
            )

    def _parse_html(self, html: str) -> Iterator[RawOrder]:
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            logger.warning("Scraper HTML: install 'beautifulsoup4' for selector support")
            return
        soup = BeautifulSoup(html, "html.parser")
        if not self.selector_goal:
            logger.warning("Scraper HTML: BRIDGE_SCRAPER_SELECTOR_GOAL required for HTML")
            return
        goals = soup.select(self.selector_goal) if self.selector_goal else []
        for i, el in enumerate(goals):
            goal = _text(el)
            if not goal:
                continue
            amount = 0
            if self.selector_amount:
                am_el = el.select_one(self.selector_amount) if hasattr(el, "select_one") else None
                if am_el:
                    amount = int(_parse_amount(_text(am_el)))
            sid_el = el.select_one(self.selector_id) if self.selector_id and hasattr(el, "select_one") else None
            source_id = _text(sid_el) or str(i)
            yield RawOrder(
                source_id=f"scraper:{source_id}",
                goal=goal[:8000],
                amount_cents=amount,
                currency="USD",
                charter="Default",
                meta={},
            )


def _parse_amount(text: str) -> int:
    m = re.search(r'\$?\s*(\d+(?:\.\d+)?)', (text or "").replace(",", ""))
    if m:
        try:
            return int(float(m.group(1)) * 100)
        except (ValueError, TypeError):
            pass
    return 0
