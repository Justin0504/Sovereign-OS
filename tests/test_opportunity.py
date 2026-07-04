"""
Tests for the CEO task-selection brain (governance/opportunity.py) and the
per-category delivery track record it reads from SovereignAuth.
"""

from sovereign_os.agents.auth import SovereignAuth
from sovereign_os.governance.opportunity import (
    evaluate_job,
    platform_economics,
    score_opportunity,
    success_probability,
)


# ---------------------------------------------------------- success probability
def test_success_probability_prior_and_evidence():
    assert abs(success_probability(0, 0) - 0.7) < 1e-9          # prior
    assert success_probability(10, 0) > success_probability(0, 0)  # passes raise it
    assert success_probability(0, 10) < success_probability(0, 0)  # fails lower it
    # monotonic in evidence
    assert success_probability(20, 0) > success_probability(5, 0)
    # bounded
    p = success_probability(1000, 0)
    assert 0.0 <= p <= 1.0


def test_success_probability_strength_controls_movement():
    # weaker prior strength moves faster with the same evidence
    fast = success_probability(3, 0, prior_strength=1.0)
    slow = success_probability(3, 0, prior_strength=10.0)
    assert fast > slow


# ------------------------------------------------------------ platform economics
def test_platform_economics_known_and_default():
    assert platform_economics("apb").currency == "USDC"
    assert platform_economics("stackstasker").network == "stacks"
    assert platform_economics("rentahuman").fee_ratio > 0  # fiat rail has a fee
    d = platform_economics("nonexistent")
    assert d.fee_ratio == 0.0 and d.gas_cents == 0


def test_platform_economics_env_override(monkeypatch):
    monkeypatch.setenv("SOVEREIGN_PLATFORM_ECON_JSON",
                       '{"apb": {"gas_cents": 42, "fee_ratio": 0.05}}')
    e = platform_economics("apb")
    assert e.gas_cents == 42 and e.fee_ratio == 0.05


# --------------------------------------------------------------- EV scoring
def test_ev_prefers_higher_success_probability():
    hi = score_opportunity(500, 20, 0.9, platform="apb")
    lo = score_opportunity(500, 20, 0.2, platform="apb")
    assert hi.expected_value_cents > lo.expected_value_cents


def test_ev_rejects_negative_expected_value():
    # cost far exceeds probability-weighted payout
    o = score_opportunity(100, 300, 0.5)
    assert o.take is False and o.expected_value_cents < 0


def test_ev_enforces_success_case_margin_floor():
    # positive EV but the best-case margin is below a 50% floor
    o = score_opportunity(100, 60, 0.95, margin_floor=0.5)
    assert o.take is False and "floor" in o.reason


def test_ev_gas_and_fee_reduce_payout():
    no_fee = score_opportunity(1000, 50, 0.9, fee_ratio=0.0, gas_cents=0)
    fee = score_opportunity(1000, 50, 0.9, fee_ratio=0.1, gas_cents=50)
    assert fee.net_margin_cents < no_fee.net_margin_cents
    assert fee.fee_cents == 100 and fee.gas_cents == 50


# ----------------------------------------------------------------- evaluate_job
def test_evaluate_job_track_record_changes_verdict_value():
    good = evaluate_job(500, "Write a blurb", "writing", platform="apb", successes=10, failures=0)
    bad = evaluate_job(500, "Write a blurb", "writing", platform="apb", successes=0, failures=8)
    assert good.expected_value_cents > bad.expected_value_cents
    assert good.success_prob > bad.success_prob


def test_evaluate_job_cheap_coding_is_skipped():
    o = evaluate_job(5, "Fix a subtle concurrency bug", "coding")
    assert o.take is False


def test_evaluate_job_reads_margin_floor_env(monkeypatch):
    monkeypatch.setenv("SOVEREIGN_MIN_MARGIN_RATIO", "0.9")
    # A thin coding bounty: LLM cost is a big fraction of the payout, so the 90%
    # success-case margin floor rejects it (floor read from env).
    o = evaluate_job(30, "Fix a bug", "coding", platform="apb", successes=5, failures=0)
    assert o.take is False and "floor" in o.reason


# --------------------------------------------------- auth per-category track record
def test_auth_records_per_category_history():
    auth = SovereignAuth()
    auth.record_audit("coder-1", passed=True, score=0.9, category="coding")
    auth.record_audit("coder-1", passed=True, score=0.8, category="coding")
    auth.record_audit("coder-1", passed=False, score=0.2, category="coding")
    auth.record_audit("writer-1", passed=True, score=0.9, category="writing")
    assert auth.category_history("coder-1", "coding") == (2, 1)
    assert auth.category_history("coder-1", "writing") == (0, 0)
    # fleet-wide aggregation across agents
    auth.record_audit("coder-2", passed=True, score=0.9, category="coding")
    assert auth.category_history_all("coding") == (3, 1)


def test_auth_category_history_persists(tmp_path):
    p = tmp_path / "trust.json"
    a1 = SovereignAuth(persist_path=str(p))
    a1.record_audit("coder-1", passed=True, score=0.9, category="coding")
    a1.record_audit("coder-1", passed=False, score=0.2, category="coding")
    a2 = SovereignAuth(persist_path=str(p))
    assert a2.category_history("coder-1", "coding") == (1, 1)
