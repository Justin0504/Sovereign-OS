"""
Tests for the governance evaluation harness.

The harness exists because the paper's headline numbers had no code behind them in this
repository — nothing a reader could clone and run. For a system whose argument is
"verify rather than trust", an unreproducible evaluation is the wrong kind of irony.

What these tests guard is the harness's honesty rather than its results. A harness that
cannot fail is not measuring anything, so the checks below are mostly about whether it
would notice if the system were broken.
"""

import pytest

from sovereign_os.bench.governance_eval import (
    FISCAL_SCENARIOS,
    run_all,
    run_fiscal_axis,
    run_integrity_axis,
    run_permission_axis,
)


# ------------------------------------------------------------------ axis 1

def test_every_fiscal_scenario_is_refused_by_the_gate_it_targets():
    result = run_fiscal_axis()
    assert result.total == 30
    assert result.passed == result.total, [d for d in result.details if not d["ok"]]


def test_a_scenario_refused_by_the_wrong_gate_would_not_count():
    """
    The expectation is named per scenario, so a refusal from a different gate scores as
    a failure. Without that, any blanket denial would read as full coverage.
    """
    assert all("expect" in sc for sc in FISCAL_SCENARIOS)
    assert {sc["expect"] for sc in FISCAL_SCENARIOS} == {
        "FiscalInsolvencyError", "UnprofitableJobError"}


def test_the_scenarios_span_the_claimed_categories():
    assert {sc["category"] for sc in FISCAL_SCENARIOS} == {
        "insufficient_balance", "min_reserve", "task_ceiling", "daily_burn", "unprofitable"}


# ------------------------------------------------------------------ axis 2

def test_permission_gating_is_consistent_with_its_own_policy():
    result = run_permission_axis(missions=200)
    assert result.total == 200
    assert result.passed == result.total


def test_the_permission_oracle_is_the_threshold_rule_not_a_label():
    """
    Each mission is scored against the threshold applied to the score the store reports.
    That measures self-consistency — a weaker claim than "the policy is correct", and
    the honest one, since a hand-labelled oracle would measure the labeller.
    """
    result = run_permission_axis(missions=20)
    for d in result.details:
        assert d["expected"] == (d["score"] >= d["threshold"])


def test_the_run_is_deterministic():
    """A benchmark that moves between runs cannot support a reported figure."""
    assert run_permission_axis(missions=50).passed == run_permission_axis(missions=50).passed


# ------------------------------------------------------------------ axis 3

def test_untampered_reports_verify_and_hashes_are_distinct():
    result = run_integrity_axis(reports=300)
    assert result.passed == 300
    probe = [d for d in result.details if "distinct_hashes" in d][0]
    assert probe["collisions"] == 0


def test_tampering_with_the_verdict_is_caught():
    """Half the axis. A verifier returning True for everything passes the other half."""
    result = run_integrity_axis(reports=300)
    probe = [d for d in result.details if "tamper_inside_canonical" in d][0]
    caught, attempted = probe["tamper_inside_canonical"].split()[0].split("/")
    assert attempted != "0" and caught == attempted


def test_the_hash_does_not_cover_who_produced_the_report():
    """
    A real limit on the tamper-evidence claim, pinned so it is not quietly forgotten:
    proof_hash covers seven fields — the verdict — and agent_id is not among them, so a
    report re-attributed to a different agent still verifies.

    Recorded rather than asserted away. If the canonical set is ever widened to cover
    attribution this test should fail, which is the point: that change breaks every
    existing trail's hashes and should not happen silently.
    """
    result = run_integrity_axis(reports=100)
    probe = [d for d in result.details if "tamper_outside_canonical" in d][0]
    caught, attempted = probe["tamper_outside_canonical"].split()[0].split("/")
    assert attempted != "0"
    assert caught == "0", (
        "attribution is now covered by the hash — update the paper's limitation and "
        "the audit-trail version, since existing trails will no longer verify")
    assert "agent_id" not in probe["covered_fields"]


# ------------------------------------------------------------------- driver

def test_run_all_reports_every_axis():
    report = run_all(missions=20, reports=50)
    assert set(report["axes"]) == {"fiscal_governance", "permission_gating",
                                   "audit_integrity"}
    for summary in report["summary"].values():
        assert "/" in summary and "%" in summary


def test_failures_are_surfaced_not_summarised_away():
    """A report that only carries a rate hides what went wrong."""
    result = run_fiscal_axis()
    assert "failures" in result.as_dict()
