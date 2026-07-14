"""
Tests for the Treasurer profit engine: budget-constrained portfolio selection and
the realized-yield reward loop feeding back into the EV brain.
"""

from sovereign_os.governance.opportunity import evaluate_job
from sovereign_os.governance.portfolio import (
    PortfolioItem,
    YieldTracker,
    lane_multiplier,
    profit_density,
    record_yield,
    reset_yield,
    select_portfolio,
)


# --------------------------------------------------------------- portfolio math
def test_profit_density():
    assert profit_density(100, 10) == 10.0
    assert profit_density(100, 0) == 100.0  # guarded divide (min cost 1)


def test_portfolio_prefers_high_density_under_budget():
    items = [
        PortfolioItem("fat", ev_cents=300, cost_cents=250),   # density 1.2
        PortfolioItem("t1", ev_cents=40, cost_cents=10),      # density 4.0
        PortfolioItem("t2", ev_cents=35, cost_cents=10),      # density 3.5
        PortfolioItem("t3", ev_cents=30, cost_cents=10),      # density 3.0
    ]
    r = select_portfolio(items, budget_cents=40)
    assert r.taken == ["t1", "t2", "t3"]      # thin, high-density jobs chosen first
    assert r.total_cost_cents == 30 and r.total_ev_cents == 105
    assert ("fat", "over budget") in r.skipped


def test_portfolio_drops_nonpositive_ev():
    items = [PortfolioItem("a", 50, 10), PortfolioItem("loss", -5, 5), PortfolioItem("zero", 0, 1)]
    r = select_portfolio(items, budget_cents=0, min_ev_cents=1.0)
    assert r.taken == ["a"]
    assert {i for i, _ in r.skipped} == {"loss", "zero"}


def test_portfolio_no_budget_takes_all_positive():
    items = [PortfolioItem("a", 10, 100), PortfolioItem("b", 20, 5)]
    r = select_portfolio(items, budget_cents=0)
    assert set(r.taken) == {"a", "b"} and r.total_cost_cents == 105


def test_portfolio_roi_and_dict():
    r = select_portfolio([PortfolioItem("a", 90, 30)], budget_cents=100)
    assert r.roi == 3.0 and r.as_dict()["count"] == 1


# ------------------------------------------------------------------ yield tracker
def test_yield_tracks_profit_and_ratio():
    y = YieldTracker()
    y.record("coding:apb", revenue_cents=500, cost_cents=100)   # +400
    y.record("coding:apb", revenue_cents=300, cost_cents=100)   # +200
    assert y.profit_of("coding:apb") == 600
    assert y.yield_of("coding:apb") == 3.0                      # 600 profit / 200 spend
    assert y.snapshot()["coding:apb"]["count"] == 2


def test_yield_multiplier_bounded_and_neutral_when_unseen():
    y = YieldTracker()
    assert y.multiplier("x:y") == 1.0                            # no evidence -> neutral
    y.record("hot:apb", revenue_cents=1000, cost_cents=100)     # yield 9.0 -> capped
    assert y.multiplier("hot:apb") == 1.25
    y.record("cold:apb", revenue_cents=0, cost_cents=200)       # yield -1.0
    assert y.multiplier("cold:apb") < 1.0 and y.multiplier("cold:apb") >= 0.75


def test_top_lanes_ranks_by_profit():
    y = YieldTracker()
    y.record("a:p", revenue_cents=100, cost_cents=10)   # +90
    y.record("b:p", revenue_cents=500, cost_cents=50)   # +450
    assert [lane for lane, _ in y.top_lanes(1)] == ["b:p"]


# --------------------------------------------------- reward loop feeds EV brain
def test_reward_loop_boosts_proven_lane_ev():
    reset_yield()
    base = evaluate_job(500, "write code", "coding", platform="apb", successes=5, failures=0)
    record_yield("coding", "apb", revenue_cents=1000, cost_cents=100)  # very profitable lane
    boosted = evaluate_job(500, "write code", "coding", platform="apb", successes=5, failures=0)
    assert boosted.expected_value_cents > base.expected_value_cents
    assert lane_multiplier("coding", "apb") > 1.0
    reset_yield()


def test_reward_loop_multiplier_never_flips_take_sign():
    reset_yield()
    # a losing lane's penalty must not turn a clearly-profitable job into a skip
    record_yield("coding", "apb", revenue_cents=0, cost_cents=500)  # lane looks bad
    o = evaluate_job(5000, "big profitable job", "writing", platform="apb", successes=9, failures=0)
    assert o.take is True  # writing lane unaffected; still taken
    reset_yield()
