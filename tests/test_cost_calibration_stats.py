"""
Tests for the estimate-vs-actual error distribution.

The headline is `test_pooled_factor_misprices_the_typical_task`: it constructs a
realistic heavy-tailed cost history and shows the deployed correction landing ~3x away
from what the typical task actually needs. That gap is a property of the estimator the
whole money stack sits on, and it is invisible to a design that keeps only running sums.
"""

import math

import pytest

from sovereign_os.governance.cost_model import (
    CalibrationSample,
    CostCalibrator,
    calibration_report,
    compute_stats,
    cost_factor,
    cost_stats,
    record_cost,
    reset_cost,
    safety_multiplier,
)


@pytest.fixture
def cal():
    return CostCalibrator()


@pytest.fixture(autouse=True)
def clean_global():
    reset_cost()
    yield
    reset_cost()


def _sample(est, act, category="coding"):
    return CalibrationSample(category=category, estimated_cents=est, actual_cents=act)


# ------------------------------------------------------------------ basic shape

def test_perfect_estimates_are_unbiased():
    stats = compute_stats([_sample(100, 100) for _ in range(10)])
    assert stats.n == 10
    assert stats.bias_log == pytest.approx(0.0)
    assert stats.geometric_mean_ratio == pytest.approx(1.0)
    assert stats.spread_log == pytest.approx(0.0)
    assert stats.under_rate == 0.0
    assert stats.within_2x == 1.0


def test_systematic_underestimate_shows_as_bias():
    """Every job costs twice the estimate: the central correction should be 2.0."""
    stats = compute_stats([_sample(50, 100) for _ in range(8)])
    assert stats.geometric_mean_ratio == pytest.approx(2.0)
    assert stats.bias_log == pytest.approx(math.log(2))
    assert stats.under_rate == 1.0
    assert stats.spread_log == pytest.approx(0.0)


def test_log_space_makes_symmetric_errors_cancel():
    """
    Half the jobs cost 2x the estimate, half cost half. The estimator is scattered but
    not biased — an average taken in raw ratio space would wrongly report 1.25.
    """
    samples = [_sample(100, 200) for _ in range(5)] + [_sample(100, 50) for _ in range(5)]
    stats = compute_stats(samples)
    assert stats.geometric_mean_ratio == pytest.approx(1.0)
    assert stats.bias_log == pytest.approx(0.0)
    assert stats.spread_log == pytest.approx(math.log(2))
    # The naive linear average of ratios would have claimed a 25% underestimate.
    assert sum(s.ratio for s in samples) / len(samples) == pytest.approx(1.25)


# --------------------------------------------------------------- the headline

def test_pooled_factor_misprices_the_typical_task(cal):
    """
    Twenty small jobs estimated perfectly, plus one large job that overran 4x — an
    ordinary shape for heavy-tailed LLM cost.

    The deployed factor is a ratio of sums, so the single large job drags it to ~3.2x
    and every ordinary task gets its estimate inflated threefold. The typical task
    needed no correction at all.
    """
    for _ in range(20):
        cal.record("coding", 10, 10)
    cal.record("coding", 1000, 4000)

    stats = cal.stats("coding")
    deployed = cal.factor("coding")

    assert stats.n == 21
    assert stats.median_ratio == pytest.approx(1.0)
    assert stats.geometric_mean_ratio == pytest.approx(1.068, abs=0.01)
    assert stats.pooled_ratio == pytest.approx(3.5)

    # The correction actually in use is ~3x what the median task requires.
    assert deployed == pytest.approx(3.19, abs=0.02)
    assert stats.divergence == pytest.approx(3.28, abs=0.05)
    assert deployed / stats.median_ratio > 3.0


def test_divergence_is_one_when_costs_are_homogeneous(cal):
    """With uniform job sizes the two estimators agree, as they should."""
    for _ in range(12):
        cal.record("writing", 100, 150)
    stats = cal.stats("writing")
    assert stats.divergence == pytest.approx(1.0, abs=0.01)


# -------------------------------------------------------------- risk, not mean

def test_safety_multiplier_exceeds_the_central_correction_under_spread():
    """
    A centered but scattered estimator: correcting the mean still leaves half the jobs
    over their ceiling, so sizing a budget needs an upper quantile.
    """
    samples = ([_sample(100, 300) for _ in range(3)]
               + [_sample(100, 100) for _ in range(4)]
               + [_sample(100, 33) for _ in range(3)])
    stats = compute_stats(samples)
    assert stats.geometric_mean_ratio == pytest.approx(1.0, abs=0.05)
    assert stats.spread_log > 0.8
    assert stats.safety_multiplier(0.8) > stats.geometric_mean_ratio
    assert stats.safety_multiplier(0.95) >= stats.safety_multiplier(0.8)


def test_safety_multiplier_falls_back_without_evidence():
    stats = compute_stats([_sample(100, 200)])
    assert stats.n == 1
    assert stats.safety_multiplier(0.8) == pytest.approx(stats.geometric_mean_ratio)


def test_overrun_rate_and_coverage():
    samples = ([_sample(100, 120) for _ in range(6)]      # over, within 2x
               + [_sample(100, 90) for _ in range(2)]     # under, within 2x
               + [_sample(100, 500) for _ in range(2)])   # over, outside 2x
    stats = compute_stats(samples)
    assert stats.under_rate == pytest.approx(0.8)
    assert stats.within_2x == pytest.approx(0.8)


# ---------------------------------------------------------- backward compatibility

def test_factor_behavior_is_unchanged(cal):
    """The tuned point correction must keep working exactly as before."""
    assert cal.factor("coding") == 1.0           # no history -> no-op
    for _ in range(6):
        cal.record("coding", 100, 200)
    # (prior 3 at 1.0 + 6 at 2.0) / 9
    assert cal.factor("coding") == pytest.approx((3 * 1.0 + 6 * 2.0) / 9, abs=1e-4)
    assert cal.samples("coding") == 6


def test_factor_clamps_are_preserved(cal):
    for _ in range(50):
        cal.record("coding", 1, 1000)
    assert cal.factor("coding") == 4.0           # hi clamp
    cal.reset()
    for _ in range(50):
        cal.record("coding", 1000, 1)
    assert cal.factor("coding") == 0.25          # lo clamp


def test_non_positive_pairs_are_kept_out_of_the_distribution(cal):
    """
    A zero-cost job carries no usable ratio, so it must not enter the log statistics —
    but the legacy running sums still see it, keeping `factor()` untouched.
    """
    cal.record("coding", 100, 200)
    cal.record("coding", 0, 50)
    cal.record("coding", 100, 0)
    assert cal.samples("coding") == 3            # legacy count sees all three
    assert cal.stats("coding").n == 1            # only one has a usable ratio


def test_empty_stats_are_neutral():
    stats = compute_stats([])
    assert stats.n == 0
    assert stats.geometric_mean_ratio == 1.0
    assert stats.divergence == 1.0
    assert stats.safety_multiplier(0.8) == 1.0


# ------------------------------------------------------------------ bookkeeping

def test_ring_buffer_bounds_retention():
    cal = CostCalibrator(max_samples=10)
    for i in range(25):
        cal.record("coding", 100, 100 + i)
    retained = cal.samples_for("coding")
    assert len(retained) == 10
    assert retained[-1].actual_cents == 124        # newest kept
    assert retained[0].actual_cents == 115         # oldest dropped
    assert cal.samples("coding") == 25             # legacy count still totals everything


def test_stratification_fields_round_trip(cal):
    cal.record("coding", 100, 150, model="claude-opus-5", complexity=1.4)
    sample = cal.samples_for("coding")[0]
    assert sample.model == "claude-opus-5"
    assert sample.complexity == pytest.approx(1.4)
    assert sample.ratio == pytest.approx(1.5)


def test_pooled_stats_span_categories(cal):
    cal.record("coding", 100, 200)
    cal.record("writing", 100, 200)
    assert cal.stats(None).n == 2
    assert cal.stats("coding").n == 1


def test_report_pairs_shape_with_deployed_factor(cal):
    for _ in range(5):
        cal.record("coding", 100, 250)
    report = cal.calibration_report()
    assert report["by_category"]["coding"]["deployed_factor"] == cal.factor("coding")
    assert report["by_category"]["coding"]["geometric_mean_ratio"] == pytest.approx(2.5)
    assert report["overall"]["n"] == 5


def test_module_level_api():
    record_cost("coding", 100, 300, model="claude-opus-5")
    assert cost_stats("coding").n == 1
    assert cost_factor("coding") > 1.0
    assert safety_multiplier("coding") > 0
    assert "by_category" in calibration_report()


def test_reset_clears_samples(cal):
    cal.record("coding", 100, 200)
    cal.reset()
    assert cal.stats("coding").n == 0
    assert cal.samples_for("coding") == []


# ------------------------------------------------------- model pricing regressions

def test_current_claude_rates_are_correct():
    """
    Rates verified against platform.claude.com/docs/en/about-claude/pricing (2026-09-17).
    Pinned because every downstream money decision — EV screening, bid floors, portfolio
    selection — is computed from these numbers.
    """
    from sovereign_os.governance.pricing import get_model_pricing

    expected = {
        "claude-opus-5": (5.00, 25.00),
        "claude-sonnet-5": (2.00, 10.00),
        "claude-sonnet-4-6": (3.00, 15.00),
        "claude-haiku-4-5": (1.00, 5.00),
        "claude-fable-5-1": (10.00, 50.00),
    }
    for model, rate in expected.items():
        assert get_model_pricing(model) == rate, model


def test_opus_45_plus_is_not_priced_at_the_retired_opus_4_rate():
    """
    Regression: "claude-opus-4" is a prefix of "claude-opus-4-5"/"-4-8", so longest-prefix
    matching charged Opus 4.5+ at the retired Opus 4 rate of $15/$75 — 3x the real $5/$25.
    A 3x cost overestimate makes the EV screen reject work that is actually profitable.
    """
    from sovereign_os.governance.pricing import get_model_pricing

    for model in ("claude-opus-4-5", "claude-opus-4-5-20251101",
                  "claude-opus-4-6", "claude-opus-4-7", "claude-opus-4-8"):
        assert get_model_pricing(model) == (5.00, 25.00), model

    # The genuinely retired ids keep their own, higher rate.
    assert get_model_pricing("claude-opus-4-1") == (15.00, 75.00)
    assert get_model_pricing("claude-opus-4-20250514") == (15.00, 75.00)


def test_default_anthropic_model_is_live_and_priced():
    """The previous default (claude-sonnet-4-20250514) was retired and 404s."""
    from sovereign_os.governance.pricing import FALLBACK_PRICING, get_model_pricing
    from sovereign_os.llm.providers import _default_model

    model = _default_model("anthropic")
    assert model == "claude-sonnet-5"
    assert get_model_pricing(model) != FALLBACK_PRICING
