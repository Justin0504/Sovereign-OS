"""
APB (Agent Payment Bounty) source: discover x402-native bounties published as
machine-readable JSON at a well-known URL, and map them into Sovereign-OS jobs.

APB is a community extension to the x402 payment standard. A publisher advertises
earnable work by serving a document at `/.well-known/bounties.json`; each bounty
declares what action earns it, the reward (amount + asset + network), and how to
claim. Because x402/USDC-on-Base is now the dominant agent-payment rail, this is the
highest-growth discovery surface for autonomous agents.

The format is young and field names vary between publishers, so parsing is tolerant:
we accept a bare list or a wrapped object, and map several common field spellings.
Amounts are normalized to USD cents — atomic units when a `decimals` field is present
(USDC has 6), otherwise treated as human decimal.

Discovery only: this emits RawOrders. Claiming/paying happens later via the x402
settlement path and is gated behind the usual LIVE flags — nothing here moves funds.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Iterator

from sovereign_os.ingest_bridge.sources.base import OrderSource, RawOrder

logger = logging.getLogger(__name__)

DEFAULT_WELL_KNOWN_PATH = "/.well-known/bounties.json"


def _http_get_json(url: str, params: dict[str, Any], headers: dict[str, str], timeout: float) -> Any:
    import requests  # type: ignore[import]

    resp = requests.get(url, params=params, headers=headers, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def apb_amount_to_cents(amount: Any, decimals: Any = None) -> int:
    """
    Normalize an APB reward amount to USD cents.

    - With `decimals` (e.g. 6 for USDC): `amount` is atomic on-chain units, so
      usd = amount / 10**decimals.
    - Without `decimals`: `amount` is a human decimal ("5" or "5.00" -> $5.00).
    Returns 0 on anything unparseable rather than raising.
    """
    try:
        val = float(amount)
    except (TypeError, ValueError):
        return 0
    if decimals is not None:
        try:
            d = int(decimals)
            if d >= 0:
                val = val / (10 ** d)
        except (TypeError, ValueError):
            pass
    return max(0, int(round(val * 100)))


def _bounty_list(doc: Any) -> list[dict[str, Any]]:
    """Extract the bounty array from a bare list or a wrapped object."""
    if isinstance(doc, list):
        return [b for b in doc if isinstance(b, dict)]
    if isinstance(doc, dict):
        for key in ("bounties", "items", "data", "x402_bounties", "results"):
            v = doc.get(key)
            if isinstance(v, list):
                return [b for b in v if isinstance(b, dict)]
    return []


def _reward_fields(b: dict[str, Any]) -> tuple[Any, Any, str, str, str]:
    """Return (amount, decimals, currency, network, pay_to) tolerating nesting/spelling."""
    reward = b.get("reward")
    if isinstance(reward, dict):
        amount = reward.get("amount", reward.get("value", 0))
        decimals = reward.get("decimals")
        currency = reward.get("currency") or reward.get("asset") or "USDC"
        network = reward.get("network") or reward.get("chain") or "base"
        pay_to = reward.get("payTo") or reward.get("pay_to") or ""
    else:
        amount = b.get("amount", b.get("reward", b.get("payout", 0)))
        decimals = b.get("decimals")
        currency = b.get("currency") or b.get("asset") or "USDC"
        network = b.get("network") or b.get("chain") or "base"
        pay_to = b.get("payTo") or b.get("pay_to") or ""
    return amount, decimals, str(currency), str(network), str(pay_to)


def parse_apb_document(doc: Any, source_url: str = "") -> list[RawOrder]:
    """
    Parse an APB `bounties.json` document into RawOrders (pure; no network).

    Skips entries without an id or without any action/description text. Amounts are
    normalized to cents; reward currency/network/claim details are carried in
    `contact` so the x402 settlement + delivery layer can act on them later.
    """
    orders: list[RawOrder] = []
    for b in _bounty_list(doc):
        bid = str(b.get("id") or b.get("bountyId") or b.get("bounty_id") or b.get("slug") or "").strip()
        if not bid:
            continue
        title = (b.get("title") or "").strip()
        action = (b.get("action") or b.get("task") or b.get("description") or "").strip()
        if not (title or action):
            continue
        if title and action and title.lower() != action.lower():
            goal = f"{title}\n\n{action}"
        else:
            goal = action or title
        amount, decimals, currency, network, pay_to = _reward_fields(b)
        amount_cents = apb_amount_to_cents(amount, decimals)
        claim = b.get("claim") or b.get("claimUrl") or b.get("claim_steps") or b.get("steps") or ""
        orders.append(RawOrder(
            source_id=f"apb:{bid}",
            goal=goal[:20_000],
            amount_cents=amount_cents,
            currency=currency or "USDC",
            charter="Default",
            meta={
                "network": network,
                "asset": currency,
                "pay_to": pay_to,
                "tags": b.get("tags") or [],
                "deadline": b.get("deadline") or b.get("expires") or b.get("expiry"),
                "source_url": source_url,
            },
            contact={
                "platform": "apb",
                "bounty_id": bid,
                "network": network,
                "asset": currency,
                "pay_to": pay_to,
                "claim": claim,
                "source_url": source_url,
            },
        ))
    return orders


class APBOrderSource(OrderSource):
    """Discovery-only: crawl publishers' `/.well-known/bounties.json` into RawOrders."""

    source_name = "apb"

    def __init__(
        self,
        publishers: list[str] | None = None,
        *,
        well_known_path: str = DEFAULT_WELL_KNOWN_PATH,
        min_amount_usd: float = 0.0,
        max_amount_usd: float = 0.0,
        limit: int = 50,
        charter: str = "Default",
        timeout: float = 15.0,
        get_json: Callable[..., Any] | None = None,
    ) -> None:
        self.publishers = [p.rstrip("/") for p in (publishers or []) if p and p.strip()]
        self.well_known_path = "/" + well_known_path.lstrip("/")
        self.min_amount_usd = max(0.0, min_amount_usd)
        self.max_amount_usd = max(0.0, max_amount_usd)
        self.limit = max(1, limit)
        self.charter = charter or "Default"
        self.timeout = timeout
        self._get_json = get_json or _http_get_json

    def _accept(self, order: RawOrder) -> bool:
        usd = order.amount_cents / 100.0
        if self.min_amount_usd > 0 and usd < self.min_amount_usd:
            return False
        if self.max_amount_usd > 0 and usd > self.max_amount_usd:
            return False
        return True

    def fetch(self) -> Iterator[RawOrder]:
        emitted = 0
        for publisher in self.publishers:
            if emitted >= self.limit:
                break
            url = f"{publisher}{self.well_known_path}"
            try:
                doc = self._get_json(url, {}, {"Accept": "application/json"}, self.timeout)
            except Exception as e:
                logger.warning("APB source: fetch failed for %s: %s", url, e)
                continue
            for order in parse_apb_document(doc, source_url=url):
                if emitted >= self.limit:
                    break
                order.charter = self.charter
                if not self._accept(order):
                    continue
                yield order
                emitted += 1
        logger.info("APB source: emitted %d order(s) from %d publisher(s)", emitted, len(self.publishers))
