"""Edge-case hardening across category / budget / permission / connector / cost code."""

import pytest

from sovereign_os.agents.auth import Capability, SovereignAuth
from sovereign_os.agents.categories import categorize, category_for_skill, get_category
from sovereign_os.connectors import dispatch, is_available, get_connector
from sovereign_os.governance.budget_policy import CategoryBudgetPolicy
from sovereign_os.ledger.unified_ledger import UnifiedLedger


# ----- categories
def test_categorize_empty_and_unknown_default_to_general():
    assert categorize("", "").key == "general"
    assert categorize("nonsense-platform-cat", "qwerty zxcv").key == "general"
    assert get_category("does-not-exist").key == "general"
    assert category_for_skill("").key == "general"


# ----- budget policy
def test_budget_policy_global_scale_and_overrides():
    base = CategoryBudgetPolicy().ceiling_cents(skill="write_article")          # $1.00 -> 100
    scaled = CategoryBudgetPolicy(global_scale=2.0).ceiling_cents(skill="write_article")
    assert scaled == base * 2
    over = CategoryBudgetPolicy(overrides={"writing": 5.0}).ceiling_cents(skill="write_article")
    assert over == 500
    # risk multiplier can be disabled
    flat = CategoryBudgetPolicy(apply_risk=False).ceiling_cents(skill="code_assistant")  # $2 base, no x1.5
    assert flat == 200


def test_budget_policy_allows_when_no_ceiling():
    pol = CategoryBudgetPolicy(overrides={"general": 0.0})
    assert pol.allows(999999, category_key="general") is True  # 0 ceiling => unlimited


# ----- per-category permissions
def test_category_trust_seeds_from_global_then_diverges():
    auth = SovereignAuth()
    a = "agent-z"
    # No category history -> seeds from global base.
    assert auth.category_trust(a, "coding") == auth.get_trust_score(a)
    auth.record_audit(a, passed=False, score=0.0, category="coding")  # tank coding only
    assert auth.category_trust(a, "coding") < auth.category_trust(a, "writing")


def test_check_permission_for_unknown_category_uses_global():
    auth = SovereignAuth(base_trust_score=70)
    assert auth.check_permission_for("a", Capability.WRITE_FILES, "never-seen") is True  # 70 >= 40


# ----- connectors
def test_connector_dispatch_unknown_and_builtin(monkeypatch):
    monkeypatch.delenv("SOVEREIGN_EMAIL_LIVE", raising=False)
    assert "error" in dispatch("totally_unknown")
    assert dispatch("send_email", to="a@b.com", subject="s", body="b")["dry_run"] is True


def test_builtin_connector_no_env_is_available():
    assert is_available(get_connector("web_fetch")) is True   # builtin, no env keys
    # figma requires FIGMA_TOKEN -> unavailable unless set
    fig = get_connector("figma")
    assert fig is not None


# ----- cost rollup
def test_cost_summary_by_category_mixed():
    led = UnifiedLedger()
    led.record_token("gpt-4o", 100, 100, estimated_usd_cents=4, category="design")
    led.record_token("gpt-4o", 100, 100, estimated_usd_cents=6, category="design")
    led.record_token("gpt-4o", 100, 100, estimated_usd_cents=2)  # uncategorized
    s = led.cost_summary()
    assert s["by_category_cents"] == {"design": 10, "uncategorized": 2}
