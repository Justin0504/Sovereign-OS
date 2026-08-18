"""
Subscription plans for the Sovereign-OS SaaS.

The product sells the governance/audit/delivery-quality control plane — NOT a cut of
any "earnings." Tenants bring their own LLM keys and (optionally) their own Stripe /
wallet, so the platform never custodies funds. Plans therefore gate *platform*
capability and safety limits, not access to money.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TenantPlan:
    name: str
    price_cents_month: int
    seats: int
    max_missions_per_day: int      # 0 = unlimited
    max_daily_spend_cents: int     # governance ceiling on the tenant's own spend
    features: frozenset[str]       # governance | audit | dashboard | backends | mcp | earning

    def allows(self, feature: str) -> bool:
        return feature in self.features


# Earning/marketplace is an opt-in advanced feature (team tier) — kept off by default
# until the live-settlement loop is proven, so the MVP never over-promises.
PLANS: dict[str, TenantPlan] = {
    "free": TenantPlan(
        name="free", price_cents_month=0, seats=1,
        max_missions_per_day=20, max_daily_spend_cents=500,
        features=frozenset({"governance", "audit", "dashboard"}),
    ),
    "pro": TenantPlan(
        name="pro", price_cents_month=4900, seats=3,
        max_missions_per_day=500, max_daily_spend_cents=20000,
        features=frozenset({"governance", "audit", "dashboard", "backends", "mcp"}),
    ),
    "team": TenantPlan(
        name="team", price_cents_month=29900, seats=10,
        max_missions_per_day=5000, max_daily_spend_cents=200000,
        features=frozenset({"governance", "audit", "dashboard", "backends", "mcp", "earning"}),
    ),
}

DEFAULT_PLAN = "free"


def get_plan(name: str | None) -> TenantPlan:
    return PLANS.get((name or DEFAULT_PLAN).strip().lower(), PLANS[DEFAULT_PLAN])
