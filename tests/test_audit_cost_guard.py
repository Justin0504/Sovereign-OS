"""Audit-cost guard: skip the LLM judge when it would cost too much vs task value."""

from sovereign_os.auditor.review_engine import should_skip_audit


def test_off_by_default(monkeypatch):
    monkeypatch.delenv("SOVEREIGN_AUDIT_MAX_COST_RATIO", raising=False)
    assert should_skip_audit(1, "gpt-4o", est_tokens=100000) is False  # never skip when unset


def test_skips_when_audit_cost_exceeds_ratio(monkeypatch):
    monkeypatch.setenv("SOVEREIGN_AUDIT_MAX_COST_RATIO", "0.1")
    # gpt-4o judging ~8000 input tokens costs ~2 cents; task worth 1 cent -> skip.
    assert should_skip_audit(1, "gpt-4o", est_tokens=8000) is True
    # task worth $5 (500 cents) -> 2c < 0.1*500=50c -> keep the LLM judge.
    assert should_skip_audit(500, "gpt-4o", est_tokens=8000) is False


def test_zero_value_and_bad_ratio(monkeypatch):
    monkeypatch.setenv("SOVEREIGN_AUDIT_MAX_COST_RATIO", "0.1")
    assert should_skip_audit(0, "gpt-4o") is False          # unknown value -> don't skip
    monkeypatch.setenv("SOVEREIGN_AUDIT_MAX_COST_RATIO", "notafloat")
    assert should_skip_audit(1, "gpt-4o") is False
