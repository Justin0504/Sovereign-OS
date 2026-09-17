"""
Tests for the calibration harness.

`test_harness_recovers_a_known_bias` and `test_estimate_is_taken_uncalibrated` are the
ones that matter: the first validates the measuring instrument against a runner whose
error is known by construction, the second pins the measurement decision that keeps the
experiment from chasing its own correction.
"""

import json
import math

import pytest

from sovereign_os.bench.calibration_run import (
    DEFAULT_SUITE,
    CalibrationTask,
    estimate_for,
    load_rows,
    run_calibration,
    write_rows,
)
from sovereign_os.governance.cost_model import CostCalibrator, record_cost, reset_cost
from sovereign_os.governance.economics import estimate_task_cost_cents


@pytest.fixture(autouse=True)
def clean_global():
    reset_cost()
    yield
    reset_cost()


@pytest.fixture
def tasks():
    return [
        CalibrationTask("a", "Write a short function.", "coding", "small"),
        CalibrationTask("b", "Summarize a topic in a paragraph.", "research", "small"),
        CalibrationTask("c", "Draft a blog post about budgets.", "writing", "medium"),
        CalibrationTask("d", "Analyze a dataset and report totals.", "data", "medium"),
    ]


def _biased_runner(multiplier: float):
    """A runner whose realized cost is a known multiple of the prediction."""
    def run(task):
        estimated, _ = estimate_for(task)
        return estimated * multiplier
    return run


# ------------------------------------------------------- validating the instrument

def test_harness_recovers_a_known_bias(tasks):
    """
    Every task costs exactly twice its estimate. If the harness is sound, the report
    must say so — before it is trusted on a runner whose error is unknown.
    """
    cal = CostCalibrator()
    rows, report = run_calibration(_biased_runner(2.0), tasks, calibrator=cal)

    assert len(rows) == 4
    assert all(r.ok for r in rows)
    assert all(r.ratio == pytest.approx(2.0) for r in rows)

    overall = report["overall"]
    assert overall["n"] == 4
    assert overall["geometric_mean_ratio"] == pytest.approx(2.0)
    assert overall["bias_log"] == pytest.approx(math.log(2), abs=1e-4)  # report rounds to 4dp
    assert overall["under_rate"] == 1.0


def test_harness_recovers_a_known_spread(tasks):
    """Symmetric multiplicative scatter: unbiased centre, non-zero spread."""
    multipliers = iter([4.0, 0.25, 4.0, 0.25])

    def run(task):
        estimated, _ = estimate_for(task)
        return estimated * next(multipliers)

    _, report = run_calibration(run, tasks, calibrator=CostCalibrator())
    overall = report["overall"]
    assert overall["geometric_mean_ratio"] == pytest.approx(1.0, abs=1e-4)
    assert overall["spread_log"] == pytest.approx(math.log(4), abs=1e-4)
    assert overall["within_2x"] == 0.0


def test_estimate_is_taken_uncalibrated(tasks):
    """
    The harness must measure the raw estimator. A learned correction may not leak into
    the prediction being evaluated, or the experiment measures its own output.
    """
    task = tasks[0]
    before, _ = estimate_for(task)

    # Teach the global calibrator a large correction for this category.
    for _ in range(40):
        record_cost("coding", 10, 40)

    after, _ = estimate_for(task)
    assert after == pytest.approx(before), "estimate_for must ignore the learned factor"

    # ...while the calibrated path does move, confirming the correction is real.
    calibrated = estimate_task_cost_cents("coding", "gpt-4o", complexity=1.0, calibrated=True)
    raw = estimate_task_cost_cents("coding", "gpt-4o", complexity=1.0, calibrated=False)
    assert calibrated > raw


# ----------------------------------------------------------------- failure handling

def test_failed_task_is_recorded_but_excluded_from_statistics(tasks):
    def run(task):
        if task.id == "b":
            raise RuntimeError("worker exploded")
        estimated, _ = estimate_for(task)
        return estimated * 2.0

    rows, report = run_calibration(run, tasks, calibrator=CostCalibrator())

    failed = [r for r in rows if not r.ok]
    assert len(failed) == 1
    assert failed[0].task_id == "b"
    assert "worker exploded" in failed[0].error
    assert failed[0].ratio is None

    # The crash says nothing about cost estimation, so it must not enter the stats.
    assert report["overall"]["n"] == 3
    assert report["overall"]["geometric_mean_ratio"] == pytest.approx(2.0)
    assert report["suite"]["failed"] == 1
    assert report["suite"]["completed"] == 3


def test_zero_cost_run_does_not_masquerade_as_a_perfect_estimate(tasks):
    """A task that somehow cost nothing carries no ratio and must not skew the centre."""
    rows, report = run_calibration(lambda t: 0.0, tasks, calibrator=CostCalibrator())
    assert all(r.ok for r in rows)
    assert report["overall"]["n"] == 0


# ------------------------------------------------------------------- persistence

def test_rows_round_trip_through_jsonl(tmp_path, tasks):
    out = tmp_path / "runs" / "calibration.jsonl"
    rows, report = run_calibration(_biased_runner(1.5), tasks,
                                   calibrator=CostCalibrator(), out_path=out)

    assert out.exists()
    reloaded = load_rows(out)
    assert [r.task_id for r in reloaded] == [r.task_id for r in rows]
    assert reloaded[0].ratio == pytest.approx(1.5)

    report_path = out.with_suffix(".report.json")
    assert json.loads(report_path.read_text())["overall"]["n"] == 4
    assert report["suite"]["tasks"] == 4


def test_write_rows_creates_parent_directories(tmp_path, tasks):
    rows, _ = run_calibration(_biased_runner(1.0), tasks, calibrator=CostCalibrator())
    path = write_rows(rows, tmp_path / "deep" / "nested" / "out.jsonl")
    assert path.exists()


# ------------------------------------------------------------------- the suite

def test_default_suite_spans_categories_and_tiers():
    """A corpus concentrated in one category would measure the category, not the estimator."""
    categories = {t.category for t in DEFAULT_SUITE}
    tiers = {t.tier for t in DEFAULT_SUITE}
    assert categories == {"coding", "research", "writing", "data", "design", "automation"}
    assert tiers == {"small", "medium", "large"}
    assert len(DEFAULT_SUITE) == 18
    assert len({t.id for t in DEFAULT_SUITE}) == 18


def test_suite_complexity_tracks_intended_tier():
    """
    The heuristic's own difficulty score should at least order the tiers correctly; if it
    does not, that is itself a finding about the estimator.
    """
    by_tier = {"small": [], "medium": [], "large": []}
    for task in DEFAULT_SUITE:
        _, complexity = estimate_for(task)
        by_tier[task.tier].append(complexity)

    mean = {k: sum(v) / len(v) for k, v in by_tier.items()}
    assert mean["small"] < mean["medium"] < mean["large"]


def test_estimates_are_positive_for_every_task():
    for task in DEFAULT_SUITE:
        estimated, complexity = estimate_for(task)
        assert estimated > 0, f"{task.id} has no usable prediction"
        assert 0.5 <= complexity <= 2.0


# ------------------------------------------------- pricing / execution-model match

def test_execution_model_follows_the_configured_provider(monkeypatch):
    """
    The estimate must be priced for the model that actually runs. An Anthropic-only
    environment executes on Claude, so that is what the harness must price against.
    """
    from sovereign_os.bench.calibration_run import resolve_execution_model

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("SOVEREIGN_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("SOVEREIGN_LLM_MODEL", raising=False)
    monkeypatch.delenv("SOVEREIGN_LLM_MODEL_WORKER", raising=False)
    monkeypatch.delenv("SOVEREIGN_LLM_PROVIDER_WORKER", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    assert resolve_execution_model().startswith("claude")

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert resolve_execution_model().startswith("gpt")


def test_explicit_model_override_is_respected(monkeypatch):
    from sovereign_os.bench.calibration_run import resolve_execution_model

    monkeypatch.setenv("SOVEREIGN_LLM_MODEL", "claude-haiku-4-5")
    assert resolve_execution_model() == "claude-haiku-4-5"


def test_unpriced_model_is_flagged_loudly():
    """
    An unpriced model falls back to a generic rate, so estimate and actual are both
    computed from a price belonging to neither. That must never pass silently.
    """
    from sovereign_os.bench.calibration_run import check_model_is_priced

    assert check_model_is_priced("gpt-4o") == ""
    assert check_model_is_priced("claude-sonnet-4-20250514") == ""

    warning = check_model_is_priced("claude-sonnet-5")
    assert "not in the pricing table" in warning
    assert "claude-sonnet-5" in warning


def test_report_carries_the_pricing_warning(tasks):
    from sovereign_os.governance.cost_model import CostCalibrator

    _, priced = run_calibration(_biased_runner(2.0), tasks,
                                model="gpt-4o", calibrator=CostCalibrator())
    assert priced["suite"]["model_priced"] is True
    assert priced["warnings"] == []

    _, unpriced = run_calibration(_biased_runner(2.0), tasks,
                                  model="claude-opus-5", calibrator=CostCalibrator())
    assert unpriced["suite"]["model_priced"] is False
    assert len(unpriced["warnings"]) == 1
