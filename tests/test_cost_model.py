"""Tests for cost calibration: learn real compute cost per category from settled jobs."""

from sovereign_os.governance.cost_model import (
    CostCalibrator,
    cost_factor,
    cost_snapshot,
    record_cost,
    reset_cost,
)
from sovereign_os.governance.economics import estimate_task_cost_cents


def test_no_history_is_neutral():
    c = CostCalibrator()
    assert c.factor("coding") == 1.0
    assert c.samples("coding") == 0


def test_factor_converges_toward_actual_ratio():
    c = CostCalibrator(prior_samples=3.0)
    for _ in range(5):
        c.record("coding", 10, 30)  # actual 3x estimate
    assert c.factor("coding") == 2.25          # (3 + 5*3)/8
    for _ in range(15):
        c.record("coding", 10, 30)
    assert 2.7 < c.factor("coding") < 3.0      # converging toward 3


def test_overestimate_lowers_factor():
    c = CostCalibrator(prior_samples=3.0)
    for _ in range(5):
        c.record("writing", 100, 50)           # actual is half
    assert c.factor("writing") == 0.6875       # (3 + 5*0.5)/8


def test_factor_is_scale_independent():
    cheap = CostCalibrator()
    dear = CostCalibrator()
    for _ in range(10):
        cheap.record("email", 2, 4)            # ratio 2, tiny cents
        dear.record("coding", 2000, 4000)      # ratio 2, big cents
    assert cheap.factor("email") == dear.factor("coding")


def test_factor_is_bounded():
    c = CostCalibrator()
    for _ in range(50):
        c.record("data", 10, 100000)           # absurd under-estimate
    assert c.factor("data") == 4.0             # clamp hi
    for _ in range(50):
        c.record("res", 100000, 1)             # absurd over-estimate
    assert c.factor("res") == 0.25             # clamp lo


def test_snapshot_shape():
    c = CostCalibrator()
    c.record("coding", 10, 30)
    snap = c.snapshot()["coding"]
    assert snap["samples"] == 1 and snap["actual_cents"] == 30 and snap["factor"] > 1.0


# --------------------------------------------------- economics integration
def test_estimator_applies_calibration():
    reset_cost()
    raw = estimate_task_cost_cents("coding", complexity=1.0, calibrated=False)
    assert estimate_task_cost_cents("coding", calibrated=True) == raw  # no history -> same
    for _ in range(8):
        record_cost("coding", raw, raw * 3)     # coding really costs ~3x
    calibrated = estimate_task_cost_cents("coding", complexity=1.0, calibrated=True)
    assert calibrated > raw                      # estimate corrected upward
    assert cost_factor("coding") > 1.0
    reset_cost()


def test_job_completion_records_actual_cost_end_to_end():
    """A settled job feeds both loops from the ledger's REAL token cost, not the estimate."""
    import sovereign_os.web.app as app
    from sovereign_os.governance.portfolio import reset_yield, yield_snapshot
    from sovereign_os.governance.strategist import PlannedTask, TaskPlan
    from sovereign_os.ledger.unified_ledger import UnifiedLedger

    reset_cost()
    reset_yield()
    saved_ledger = app._ledger
    try:
        led = UnifiedLedger()
        led.record_usd(10000)
        led.record_token(model_id="gpt-4o", input_tokens=8000, output_tokens=6000,
                         task_id="t1", estimated_usd_cents=60, category="coding")
        app._ledger = led

        class _Job:
            goal = "Fix a concurrency bug"
            amount_cents = 500
            delivery_contact = {"platform": "apb"}

        plan = TaskPlan(tasks=[PlannedTask(task_id="t1", required_skill="code_assistant", description="x")])
        app._record_job_economics(_Job(), plan)

        cal = cost_snapshot()["coding"]
        assert cal["actual_cents"] == 60 and cal["samples"] == 1 and cal["factor"] > 1.0
        lane = yield_snapshot()["coding:apb"]
        assert lane["spend_cents"] == 60 and lane["profit_cents"] == 440  # revenue 500 − actual 60
    finally:
        app._ledger = saved_ledger
        reset_cost()
        reset_yield()


def test_raw_estimate_ignores_calibration():
    reset_cost()
    for _ in range(10):
        record_cost("coding", 10, 30)
    raw = estimate_task_cost_cents("coding", calibrated=False)
    # raw must equal a fresh raw computation regardless of recorded history
    reset_cost()
    assert estimate_task_cost_cents("coding", calibrated=False) == raw
    reset_cost()
