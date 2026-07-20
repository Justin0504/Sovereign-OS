"""Tests for the capital allocator: split compute budget across lanes by ROI."""

from sovereign_os.governance.allocator import allocate_budget, plan_allocation


def test_budget_fully_used_and_favors_high_yield():
    a = allocate_budget(1000, {"hi:p": 5.0, "lo:p": 1.0, "zero:p": 0.0}, exploration_frac=0.2)
    assert sum(a.values()) == 1000               # whole budget allocated
    assert a["hi:p"] > a["lo:p"] > a["zero:p"]   # more to proven lanes


def test_exploration_keeps_every_lane_alive():
    a = allocate_budget(1000, {"hi:p": 100.0, "dead:p": 0.0}, exploration_frac=0.2)
    assert a["dead:p"] > 0                        # never starved to zero


def test_cold_start_even_split_when_no_positive_yield():
    a = allocate_budget(900, {"a:p": 0.0, "b:p": -2.0, "c:p": 0.0})
    assert a == {"a:p": 300, "b:p": 300, "c:p": 300}


def test_zero_exploration_is_pure_exploit():
    a = allocate_budget(1000, {"hi:p": 3.0, "lo:p": 1.0}, exploration_frac=0.0)
    assert a["lo:p"] == 0 or a["hi:p"] > a["lo:p"]  # (rounding remainder may seed one)
    assert sum(a.values()) == 1000


def test_empty_and_zero():
    assert allocate_budget(1000, {}) == {}
    assert allocate_budget(0, {"a:p": 1.0}) == {}


def test_proportional_to_yield():
    a = allocate_budget(1000, {"x:p": 3.0, "y:p": 1.0}, exploration_frac=0.0)
    # x has 3x the yield of y -> roughly 3x the budget
    assert a["x:p"] > 2.5 * a["y:p"]


def test_plan_allocation_reads_global_yields():
    from sovereign_os.governance.portfolio import record_yield, reset_yield

    reset_yield()
    record_yield("coding", "apb", revenue_cents=1000, cost_cents=100)   # yield 9
    record_yield("writing", "apb", revenue_cents=150, cost_cents=100)   # yield 0.5
    a = plan_allocation(1000, exploration_frac=0.2)
    assert a and a["coding:apb"] > a["writing:apb"]
    assert sum(a.values()) == 1000
    reset_yield()
