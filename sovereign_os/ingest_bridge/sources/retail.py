"""
Retail source: fetch orders from Shopify (or WooCommerce) and map to goals.
Install: pip install requests. Configure BRIDGE_RETAIL_* env vars.
"""

from __future__ import annotations

import logging
from typing import Iterator

from sovereign_os.ingest_bridge.sources.base import RawOrder, OrderSource

logger = logging.getLogger(__name__)


class RetailOrderSource(OrderSource):
    source_name = "retail"

    def __init__(self, provider: str, api_url: str, api_key: str, store_domain: str = "",
                 order_to_goal_template: str = "Order #{order_id}: {title}"):
        self.provider = provider.lower()
        self.api_url = api_url.rstrip("/")
        self.api_key = api_key
        self.store_domain = store_domain
        self.template = order_to_goal_template

    def fetch(self) -> Iterator[RawOrder]:
        if not self.api_url or not self.api_key:
            logger.warning("Retail source: BRIDGE_RETAIL_API_URL and BRIDGE_RETAIL_API_KEY required")
            return
        if self.provider == "shopify":
            yield from self._fetch_shopify()
        elif self.provider == "woocommerce":
            yield from self._fetch_woocommerce()
        else:
            logger.warning("Retail source: unknown provider %s", self.provider)

    def _fetch_shopify(self) -> Iterator[RawOrder]:
        try:
            import requests
        except ImportError:
            logger.warning("Retail source: install 'requests'")
            return
        # Shopify Admin API: GET /orders.json?status=open&limit=50
        url = f"{self.api_url}/orders.json"
        params = {"status": "open", "limit": 50}
        headers = {"X-Shopify-Access-Token": self.api_key, "Content-Type": "application/json"}
        try:
            r = requests.get(url, params=params, headers=headers, timeout=30)
            r.raise_for_status()
            data = r.json()
            orders = data.get("orders") or []
            for o in orders:
                oid = o.get("id") or o.get("name") or ""
                title = o.get("name") or f"Order {oid}"
                note = (o.get("note") or "").strip()[:500]
                goal = self.template.format(order_id=oid, title=title)
                if note:
                    goal += f". Note: {note}"
                total = 0
                try:
                    total = int(float(o.get("total_price", 0) or 0) * 100)
                except (TypeError, ValueError):
                    pass
                yield RawOrder(
                    source_id=f"shopify:{oid}",
                    goal=goal[:8000],
                    amount_cents=total,
                    currency=str(o.get("currency", "USD")).upper() or "USD",
                    charter="Default",
                    meta={"order": o.get("id"), "email": o.get("email")},
                )
        except Exception as e:
            logger.exception("Shopify fetch: %s", e)

    def _fetch_woocommerce(self) -> Iterator[RawOrder]:
        try:
            import requests
        except ImportError:
            return
        # WooCommerce REST: GET /orders?status=processing
        url = f"{self.api_url}/orders"
        params = {"status": "processing", "per_page": 50}
        auth = None
        if ":" in self.api_key:
            consumer_key, consumer_secret = self.api_key.split(":", 1)
            auth = (consumer_key, consumer_secret)
        headers = {"Content-Type": "application/json"}
        try:
            r = requests.get(url, params=params, auth=auth, headers=headers, timeout=30)
            r.raise_for_status()
            data = r.json()
            orders = data if isinstance(data, list) else []
            for o in orders:
                oid = o.get("id") or o.get("number") or ""
                title = f"Order #{oid}"
                goal = self.template.format(order_id=oid, title=title)
                total = 0
                try:
                    total = int(float(o.get("total", 0) or 0) * 100)
                except (TypeError, ValueError):
                    pass
                yield RawOrder(
                    source_id=f"woo:{oid}",
                    goal=goal[:8000],
                    amount_cents=total,
                    currency=str(o.get("currency", "USD")).upper() or "USD",
                    charter="Default",
                    meta={"order_id": oid},
                )
        except Exception as e:
            logger.exception("WooCommerce fetch: %s", e)
