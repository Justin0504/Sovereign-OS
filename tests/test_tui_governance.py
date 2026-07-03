"""
Tests for TUI governance visualization:
  - DecisionStream.format_rubric (per-criterion bars)
  - FinancePanel breaker + JIT lease reactives
  - engine task_audited event carries sub_scores + category
  - FinancePanel.render produces valid Rich markup
"""

import pytest

from sovereign_os.agents.auth import Capability, SovereignAuth
from sovereign_os.governance.circuit_breaker import SpendCircuitBreaker
from sovereign_os.ledger.unified_ledger import UnifiedLedger
from sovereign_os.ui.widgets.decision_stream import format_rubric, _mini_bar, _score_color
from sovereign_os.ui.widgets.finance_panel import FinancePanel


# ------------------------------------------------------------- rubric formatter
def test_mini_bar_scales_and_clamps():
    assert _mini_bar(1.0) == "█" * 8
    assert _mini_bar(0.0) == "░" * 8
    assert _mini_bar(0.5).count("█") == 4
    assert _mini_bar(5.0) == "█" * 8   # clamps above 1.0
    assert _mini_bar(-1.0) == "░" * 8  # clamps below 0.0


def test_score_color_bands():
    assert _score_color(0.95) == "green3"
    assert _score_color(0.75) == "gold1"
    assert _score_color(0.55) == "dark_orange"
    assert _score_color(0.2) == "red1"


def test_format_rubric_lists_each_criterion():
    out = format_rubric({"correctness": 0.9, "robustness": 0.8}, "coding")
    assert "coding rubric" in out
    assert "correctness" in out and "robustness" in out
    assert "90" in out and "80" in out
    assert "█" in out  # has bars


def test_format_rubric_empty_when_no_subscores():
    assert format_rubric({}, "coding") == ""
    assert format_rubric(None, "coding") == ""


# ------------------------------------------------------------------ FinancePanel
def test_finance_panel_reflects_breaker_and_leases():
    led = UnifiedLedger(); led.record_usd(1000)
    auth = SovereignAuth(base_trust_score=90)
    auth.grant_lease("coder-1", Capability.EXECUTE_SHELL, task_id="t1", ttl_seconds=60, max_uses=2)
    br = SpendCircuitBreaker(session_ceiling_cents=500, max_consecutive_failures=3)
    br.record_spend(300); br.record_revenue(900); br.record_outcome(False)

    fp = FinancePanel()
    fp.set_ledger(led); fp.set_auth(auth); fp.set_breaker(br)
    fp.refresh_from_backend()

    assert fp.balance_usd == "$10.00"
    assert fp.breaker_state == "CLOSED"
    assert fp.session_spend == "$3.00 / $5.00"
    assert fp.fail_streak == "1"
    assert fp.roi == "3.00"
    assert fp.lease_count == 1
    assert "execute_shell" in fp.lease_lines and "coder-1" in fp.lease_lines


def test_finance_panel_shows_tripped_state():
    br = SpendCircuitBreaker(session_ceiling_cents=100)
    br.record_spend(100)
    try:
        br.check()
    except Exception:
        pass  # trips
    fp = FinancePanel(); fp.set_breaker(br)
    fp.refresh_from_backend()
    assert fp.breaker_state == "TRIPPED"


def test_finance_panel_render_is_valid_markup():
    from rich.console import Console

    led = UnifiedLedger(); led.record_usd(500)
    fp = FinancePanel()
    fp.set_ledger(led)
    fp.set_breaker(SpendCircuitBreaker(session_ceiling_cents=500))
    fp.refresh_from_backend()
    # Rendering exercises Text.from_markup — invalid markup would raise here.
    Console(file=open("/dev/null", "w")).print(fp.render())


# --------------------------------------------------------- engine event payload
@pytest.mark.asyncio
async def test_task_audited_event_carries_rubric_breakdown():
    from sovereign_os.auditor import ReviewEngine
    from sovereign_os.auditor.base import AuditReport, BaseAuditor
    from sovereign_os.governance.engine import GovernanceEngine
    from sovereign_os.models.charter import Charter

    class RubricJudge(BaseAuditor):
        async def evaluate(self, task_id, task_output, verification_prompt, kpi_name,
                           *, min_score=None, category=None):
            return AuditReport(task_id=task_id, kpi_name=kpi_name or "d", passed=True,
                               score=0.88, reason="ok", suggested_fix="",
                               sub_scores={"correctness": 0.9, "robustness": 0.8, "safety": 1.0})

    events = []
    led = UnifiedLedger(); led.record_usd(1000)
    charter = Charter(mission="m")
    engine = GovernanceEngine(
        charter, led, auth=SovereignAuth(),
        review_engine=ReviewEngine(charter, judge=RubricJudge()),
        on_event=lambda e, d: events.append((e, d)),
    )
    await engine.run_mission_with_audit("Fix a bug in the parser", abort_on_audit_failure=False)
    audited = [d for e, d in events if e == "task_audited"]
    assert audited, "expected a task_audited event"
    assert audited[0]["sub_scores"] == {"correctness": 0.9, "robustness": 0.8, "safety": 1.0}
    assert audited[0]["category"] == "coding"


# ------------------------------------------------------------- full TUI harness
@pytest.mark.asyncio
async def test_dashboard_mounts_and_reset_breaker_action():
    from sovereign_os.ui.app import DashboardApp
    from sovereign_os.ui.widgets.finance_panel import FinancePanel

    led = UnifiedLedger(); led.record_usd(1000)
    br = SpendCircuitBreaker(session_ceiling_cents=500)
    br.record_spend(120)
    app = DashboardApp(charter_name="Test", ledger=led, auth=SovereignAuth(), engine=None, breaker=br)
    async with app.run_test() as pilot:
        app._refresh_finance()
        fp = app.query_one(FinancePanel)
        assert fp.session_spend == "$1.20 / $5.00"
        await pilot.press("b")  # reset breaker
        assert br.spent_cents == 0
        assert fp.session_spend == "$0.00 / $5.00"
