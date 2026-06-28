"""
CategoryBudgetPolicy — category- and risk-aware per-task budget ceilings.

The flat `max_task_cost_usd` charter cap treats a $0.20 summary and a $3 coding
job the same. This policy sets a ceiling per task *category* (from the platform's
own categories), scaled by a risk multiplier and a global scale knob, so the CFO
allocates more budget to higher-value categories and clamps low-value ones.

Treasury consults it (when set) in addition to the global cap — the tighter of
the two wins. Backward compatible: no policy => unchanged behavior.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sovereign_os.agents.categories import TaskCategory, categorize, category_for_skill, get_category

# Risk tier -> budget multiplier (higher-risk work is allowed a larger envelope,
# but pairs with stricter permission gates — see SovereignAuth per-category tiers).
DEFAULT_RISK_MULTIPLIERS: dict[str, float] = {"low": 1.0, "medium": 1.5, "high": 2.0}


@dataclass
class CategoryBudgetPolicy:
    """Per-category USD ceilings, risk-scaled. `overrides` replace a category's base ceiling."""

    overrides: dict[str, float] = field(default_factory=dict)
    risk_multipliers: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_RISK_MULTIPLIERS))
    global_scale: float = 1.0
    apply_risk: bool = True

    def ceiling_usd_for_category(self, category: TaskCategory) -> float:
        base = self.overrides.get(category.key, category.max_cost_usd)
        mult = self.risk_multipliers.get(category.risk, 1.0) if self.apply_risk else 1.0
        return max(0.0, base * mult * self.global_scale)

    def ceiling_cents_for_category(self, category: TaskCategory) -> int:
        return int(round(self.ceiling_usd_for_category(category) * 100))

    # --- resolve a category from whatever the caller has -------------------
    def ceiling_cents(self, *, category_key: str = "", skill: str = "",
                      platform_category: str = "", text: str = "") -> int:
        """Resolve the per-task ceiling (cents) from a category key, a worker skill, or task text."""
        if category_key:
            cat = get_category(category_key)
        elif skill:
            cat = category_for_skill(skill)
        else:
            cat = categorize(platform_category, text)
        return self.ceiling_cents_for_category(cat)

    def allows(self, estimated_cents: int, **kw) -> bool:
        ceiling = self.ceiling_cents(**kw)
        return ceiling <= 0 or estimated_cents <= ceiling
