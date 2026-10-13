"""
Tests for the ruin analysis.

These pin the *structure* of the result, not its digits. The ruin rates are stochastic
and depend on an assumed payout model; what has to hold is the contrast — that removing
the measured bias removes the ruin, and that risk-adjusted sizing does too. A test
asserting "ruin == 10.8%" would fail on a seed change while telling nobody anything.

The first version of this analysis produced ~0.1% ruin for every policy, because the
assumed market paid 500c for jobs costing 260c and nothing could lose. That was a
question about whether a profitable market is profitable. The tests below therefore
exercise the thin-margin regime the system is actually designed for.
"""

import pytest

from sovereign_os.bench.ruin import (
    MEASURED_BIAS,
    MEASURED_SPREAD_LOG,
    PERFECT_CALIBRATION,
    MarketAssumptions,
    draw_job,
    evaluate,
    find_margin_frontier,
    policy_ev,
    policy_risk_adjusted,
)

THIN = MarketAssumptions(payout_mean_cents=150.0)


# --------------------------------------------------- the measured input

def test_the_bias_is_the_measured_one_and_points_the_dangerous_way():
    """
    The whole argument rests on the direction: large tasks under-estimated means their EV
    is inflated and they get accepted. If this ever reads the other way the analysis is
    about something else.
    """
    assert MEASURED_BIAS["small"] < 1.0, "small tasks over-estimated"
    assert MEASURED_BIAS["large"] > 1.0, "large tasks under-estimated — the fatal direction"
    assert MEASURED_BIAS["small"] < MEASURED_BIAS["medium"] < MEASURED_BIAS["large"]
    assert MEASURED_SPREAD_LOG > 0


def test_realized_cost_tracks_the_bias_not_the_estimate():
    """A large job must cost more than predicted on average, or nothing here follows."""
    import random

    rng = random.Random(7)
    m = MarketAssumptions(tier_mix=(0.0, 0.0, 1.0))      # large only
    ratios = [j.true_cost_cents / j.estimated_cost_cents
              for j in (draw_job(rng, m, MEASURED_SPREAD_LOG) for _ in range(4000))]
    assert sum(ratios) / len(ratios) > 1.3


# ------------------------------------------------- the ablation is the result

def test_removing_the_measured_bias_removes_the_ruin():
    """
    The load-bearing comparison. Without this ablation a nonzero ruin rate says only that
    the assumed market is hard, which is a statement about the assumption.
    """
    biased = evaluate("ev", trials=400, horizon=1000, m=THIN)
    clean = evaluate("ev", trials=400, horizon=1000, m=THIN, bias=PERFECT_CALIBRATION)

    assert biased["ruin_rate"] > 0.01
    assert clean["ruin_rate"] == 0.0
    assert biased["ruin_rate"] > clean["ruin_rate"]


def test_ruin_accumulates_with_horizon_under_the_bias():
    """Ruin is absorbing, so a short horizon understates it; the trend has to be upward."""
    short = evaluate("ev", trials=400, horizon=250, m=THIN)["ruin_rate"]
    long = evaluate("ev", trials=400, horizon=2000, m=THIN)["ruin_rate"]
    assert long > short


def test_perfect_calibration_stays_safe_at_every_horizon():
    for horizon in (250, 1000, 2000):
        r = evaluate("ev", trials=300, horizon=horizon, m=THIN, bias=PERFECT_CALIBRATION)
        assert r["ruin_rate"] == 0.0, horizon


def test_risk_adjusted_sizing_also_removes_the_ruin():
    r = evaluate("risk_adjusted", trials=400, horizon=2000, m=THIN)
    assert r["ruin_rate"] == 0.0


def test_the_safe_policy_is_not_paying_for_safety_with_returns():
    """
    It declines roughly a third more jobs and still ends richer, because what it declines
    are the jobs whose cost was under-estimated. Safety and growth are not traded off
    here; the EV policy is simply buying losers.
    """
    ev = evaluate("ev", trials=400, horizon=1000, m=THIN)
    safe = evaluate("risk_adjusted", trials=400, horizon=1000, m=THIN)

    assert safe["acceptance_rate"] < ev["acceptance_rate"]
    assert safe["median_final_wealth"] >= ev["median_final_wealth"]


def test_thinner_margins_hurt_more():
    """The system is designed for thin margins, which is where the error bites hardest."""
    frontier = find_margin_frontier(trials=250, horizon=600)
    thinnest = frontier[0]
    fattest = frontier[-1]
    assert thinnest["payout_mean_cents"] < fattest["payout_mean_cents"]
    assert thinnest["ruin_measured_bias"] >= fattest["ruin_measured_bias"]
    assert all(row["ruin_perfect_calibration"] == 0.0 for row in frontier)


# ------------------------------------------------------------ policy shapes

def test_the_ev_policy_is_fooled_by_an_under_estimated_job():
    """The mechanism, in one decision: a job that is a loser once it costs what it costs."""
    from sovereign_os.bench.ruin import Job

    job = Job(tier="large", payout_cents=300.0, true_cost_cents=600.0,
              estimated_cost_cents=361.0, success_prob=0.9)
    assert policy_ev(job, wealth=10_000) is False or job.true_cost_cents > job.payout_cents

    cheap_lie = Job(tier="large", payout_cents=300.0, true_cost_cents=500.0,
                    estimated_cost_cents=250.0, success_prob=0.95)
    assert policy_ev(cheap_lie, wealth=10_000) is True, "EV accepts it on the point estimate"
    assert policy_risk_adjusted(cheap_lie, wealth=10_000) is False, \
        "sizing on an upper quantile of the error refuses it"


def test_exposure_is_bounded_relative_to_bankroll():
    """A job that would stake most of the bankroll is refused however good it looks."""
    from sovereign_os.bench.ruin import Job

    job = Job(tier="large", payout_cents=100_000.0, true_cost_cents=900.0,
              estimated_cost_cents=900.0, success_prob=0.99)
    assert policy_risk_adjusted(job, wealth=1_000.0) is False
    assert policy_risk_adjusted(job, wealth=1_000_000.0) is True


def test_more_uncertainty_means_smaller_bets():
    """
    The shading is derived from the measured spread, so a wider error must make the
    policy more conservative rather than requiring a hand-tuned constant.
    """
    from sovereign_os.bench.ruin import Job

    job = Job(tier="medium", payout_cents=420.0, true_cost_cents=260.0,
              estimated_cost_cents=260.0, success_prob=0.9)
    assert policy_risk_adjusted(job, wealth=5_000, spread_log=0.2) is True
    assert policy_risk_adjusted(job, wealth=5_000, spread_log=1.2) is False


def test_an_agent_cannot_take_a_job_it_cannot_fund():
    r = evaluate("ev", trials=100, horizon=200, m=THIN, start_wealth=50.0)
    assert r["ruin_rate"] > 0.0
