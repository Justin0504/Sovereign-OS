"""
Tests for the category-driven architecture: routing, top-tier workers,
category-aware budget policy, per-category permission trust, and connectors.
"""

import pytest

from sovereign_os.agents.auth import Capability, SovereignAuth
from sovereign_os.agents.base import TaskInput
from sovereign_os.agents.categories import (
    categorize,
    category_for_skill,
    get_category,
    route_skill,
)
from sovereign_os.agents.specialist_workers import DataAnalysisWorker, DesignBriefWorker
from sovereign_os.connectors import (
    connectors_for_category,
    coverage_report,
    readiness_for_category,
    required_mcp_servers,
)
from sovereign_os.governance.budget_policy import CategoryBudgetPolicy
from sovereign_os.governance.exceptions import FiscalInsolvencyError
from sovereign_os.governance.treasury import Treasury
from sovereign_os.ledger.unified_ledger import UnifiedLedger
from sovereign_os.models.charter import Charter


# ---------------------------------------------------------------- categories
def test_categorize_by_platform_label_and_text():
    assert categorize("Bug Fix").key == "coding"          # platform label
    assert categorize("", "Write a blog post about AI").key == "writing"
    assert categorize("", "research the BNPL landscape").key == "research"
    assert categorize("design", "make a logo").key == "design"
    assert categorize("", "send a cold email sequence").key == "email"
    assert categorize("", "analyze this CSV dataset").key == "data"
    assert categorize("", "just say hi").key == "general"


def test_route_skill_and_reverse_lookup():
    assert route_skill("Bug Fix") == "code_assistant"
    assert route_skill("", "write an article") == "write_article"
    assert category_for_skill("code_review").key == "coding"
    assert category_for_skill("write_email").key == "email"
    assert category_for_skill("unknown_skill").key == "general"


# -------------------------------------------------------------- top-tier workers
@pytest.mark.asyncio
async def test_specialist_workers_run_without_llm():
    d = DesignBriefWorker(agent_id="d1", system_prompt="")
    r = await d.execute(TaskInput(task_id="t1", description="Design a settings page"))
    assert r.success and "DesignBriefWorker" in r.metadata["worker"]

    da = DataAnalysisWorker(agent_id="a1", system_prompt="")
    r2 = await da.execute(TaskInput(task_id="t2", description="Analyze churn"))
    assert r2.success and "DataAnalysisWorker" in r2.metadata["worker"]


def test_engine_registers_specialist_skills():
    from sovereign_os.governance.engine import GovernanceEngine
    led = UnifiedLedger(); led.record_usd(1000)
    eng = GovernanceEngine(Charter(mission="m"), led)
    bidders = {k for k in ("design_brief", "data_analysis")}
    for skill in bidders:
        assert eng._registry.get_bidders(skill)  # a worker is registered


# --------------------------------------------------------------- budget policy
def test_budget_policy_category_ceilings():
    pol = CategoryBudgetPolicy()
    # coding (medium risk, $2 base x1.5) vs writing (low risk, $1 base x1.0)
    coding = pol.ceiling_cents(skill="code_assistant")
    writing = pol.ceiling_cents(skill="write_article")
    assert coding == 300 and writing == 100
    assert pol.allows(250, skill="code_assistant") is True
    assert pol.allows(250, skill="write_article") is False   # over the writing ceiling


def test_treasury_enforces_category_ceiling():
    led = UnifiedLedger(); led.record_usd(100000)  # plenty of balance
    t = Treasury(Charter(mission="m"), led, budget_policy=CategoryBudgetPolicy())
    t.approve_task(80, task_id="ok", skill="write_article")     # under $1 writing ceiling
    with pytest.raises(FiscalInsolvencyError):
        t.approve_task(250, task_id="too-big", skill="write_article")  # over writing ceiling
    t.approve_task(250, task_id="coding-ok", skill="code_assistant")   # fine for coding ($3)


# ----------------------------------------------------- per-category permissions
def test_per_category_trust_is_earned_separately():
    auth = SovereignAuth()
    a = "writer-1"
    # Build category trust in "writing" only.
    for _ in range(8):
        auth.record_audit(a, passed=True, category="writing")
    assert auth.category_trust(a, "writing") > auth.category_trust(a, "coding")
    assert auth.effective_trust(a, "writing") >= 80
    # WRITE_FILES (threshold 40) granted for writing where trust was earned.
    assert auth.check_permission_for(a, Capability.WRITE_FILES, "writing") is True


def test_category_spend_ceiling_uses_category_trust():
    auth = SovereignAuth()
    a = "agent-x"
    while auth.category_trust(a, "coding") < 100:
        auth.record_audit(a, passed=True, category="coding")
    assert auth.max_spend_cents_for(a, "coding") > auth.max_spend_cents_for(a, "design")


def test_category_trust_persists(tmp_path):
    p = tmp_path / "auth.json"
    auth = SovereignAuth(persist_path=p)
    auth.record_audit("a1", passed=True, score=1.0, category="research")
    val = auth.category_trust("a1", "research")
    auth2 = SovereignAuth(persist_path=p)
    assert auth2.category_trust("a1", "research") == val


# ----------------------------------------------------------------- connectors
def test_connectors_for_category_and_readiness():
    research = connectors_for_category(get_category("research"))
    names = {c.name for c in research}
    assert "web_search" in names and "web_fetch" in names
    ready = readiness_for_category("research")
    assert ready["web_fetch"] is True          # built-in, no env -> available
    assert "figma" in {c.name for c in connectors_for_category("design")}


def test_required_mcp_servers_and_coverage():
    servers = required_mcp_servers()
    assert "git" in servers and "search" in servers
    cov = coverage_report()
    assert cov["coding"]["connectors"] and "email" in cov
