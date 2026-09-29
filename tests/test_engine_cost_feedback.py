"""
Tests for the estimate→actual loop inside the governance engine.

The engine has always held both numbers for every task and used them only to dock
TrustScore on an overrun. These pin the two halves of actually closing the loop: every
settled task teaches the calibrator, and the next pre-flight estimate is moved by what it
learned. Cold start must stay a no-op, because the point is to improve the estimate, not
to change behaviour before there is any evidence.
"""

import pytest

from sovereign_os.governance.cost_model import cost_factor, cost_stats, reset_cost
from sovereign_os.governance.strategist import PlannedTask


@pytest.fixture(autouse=True)
def clean_global():
    reset_cost()
    yield
    reset_cost()


@pytest.fixture
def engine():
    from sovereign_os.governance.engine import GovernanceEngine
    from sovereign_os.ledger.unified_ledger import UnifiedLedger
    from sovereign_os.models.charter import load_charter

    return GovernanceEngine(load_charter("charter.default.yaml"), UnifiedLedger())


# Large enough that the estimate sits well clear of the 1-cent floor, or a downward
# correction has nowhere to go and the test measures the clamp instead of the loop.
def _task(skill="coding", budget=400_000, task_id="t1"):
    return PlannedTask(task_id=task_id, description="d", required_skill=skill,
                       estimated_token_budget=budget, priority="low")


# ------------------------------------------------------------- feeding the loop

def test_settled_task_teaches_the_calibrator(engine):
    engine._task_estimate_cents["t1"] = 100
    engine._task_skill["t1"] = "coding"

    engine._reconcile_cost("t1", "worker-1", 50)

    stats = cost_stats("coding")
    assert stats.n == 1
    assert stats.geometric_mean_ratio == pytest.approx(0.5)


def test_an_on_budget_task_is_recorded_too(engine):
    """
    The loop must not learn only from failures. Recording only overruns would teach the
    calibrator that every task overruns.
    """
    engine._task_estimate_cents["t1"] = 100
    engine._task_skill["t1"] = "coding"

    engine._reconcile_cost("t1", "worker-1", 100)     # exactly on budget, no overrun

    assert cost_stats("coding").n == 1


def test_zero_actual_is_not_recorded(engine):
    """A task that cost nothing carries no usable ratio."""
    engine._task_estimate_cents["t1"] = 100
    engine._task_skill["t1"] = "coding"
    engine._reconcile_cost("t1", "worker-1", 0)
    assert cost_stats("coding").n == 0


def test_task_without_an_estimate_is_skipped(engine):
    engine._reconcile_cost("unknown", "worker-1", 50)
    assert cost_stats(None).n == 0


def test_missing_skill_falls_back_to_general(engine):
    engine._task_estimate_cents["t1"] = 100
    engine._reconcile_cost("t1", "worker-1", 50)
    assert cost_stats("general").n == 1


def test_recording_failure_never_breaks_a_mission(engine, monkeypatch):
    """Bookkeeping is best-effort; a broken calibrator must not abort work."""
    import sovereign_os.governance.cost_model as cm

    def boom(*a, **k):
        raise RuntimeError("calibrator down")

    monkeypatch.setattr(cm, "record_cost", boom)
    engine._task_estimate_cents["t1"] = 100
    engine._task_skill["t1"] = "coding"
    engine._reconcile_cost("t1", "worker-1", 50)      # must not raise


# ------------------------------------------------------------- using the loop

def test_cold_start_is_an_exact_no_op(engine):
    """With no history the correction is 1.0, so behaviour is unchanged."""
    from sovereign_os.governance.pricing import estimate_budget_cost_cents, output_ratio_for_skill

    task = _task()
    model = engine._treasury.get_optimal_model("low")
    raw = estimate_budget_cost_cents(model, 400_000,
                                     output_ratio=output_ratio_for_skill("coding"))
    assert cost_factor("coding") == 1.0
    assert engine._default_cost_converter(task) == raw


def test_a_learned_over_ask_lowers_the_reserved_budget(engine):
    """
    The measured case: the planner over-asks ~2x. Once settled tasks say so, the gate
    must stop reserving twice what the work needs — that is the whole point, since
    over-reserving fits half as much work under any cap.
    """
    before = engine._default_cost_converter(_task())

    for i in range(30):                                # every task cost half its estimate
        engine._task_estimate_cents[f"t{i}"] = 100
        engine._task_skill[f"t{i}"] = "coding"
        engine._reconcile_cost(f"t{i}", "worker-1", 50)

    after = engine._default_cost_converter(_task())
    assert cost_factor("coding") < 0.75
    assert after < before
    assert after == pytest.approx(before * cost_factor("coding"), rel=0.05)


def test_a_learned_underestimate_raises_the_reserved_budget(engine):
    """The correction has to work in both directions, not just downward."""
    before = engine._default_cost_converter(_task())
    for i in range(30):
        engine._task_estimate_cents[f"t{i}"] = 100
        engine._task_skill[f"t{i}"] = "coding"
        engine._reconcile_cost(f"t{i}", "worker-1", 300)
    assert engine._default_cost_converter(_task()) > before


def test_correction_is_scoped_per_category(engine):
    """Learning about coding must not silently reprice writing."""
    writing_before = engine._default_cost_converter(_task(skill="writing"))
    for i in range(30):
        engine._task_estimate_cents[f"t{i}"] = 100
        engine._task_skill[f"t{i}"] = "coding"
        engine._reconcile_cost(f"t{i}", "worker-1", 50)
    assert engine._default_cost_converter(_task(skill="writing")) == writing_before


def test_estimate_never_falls_below_one_cent(engine):
    for i in range(50):
        engine._task_estimate_cents[f"t{i}"] = 1000
        engine._task_skill[f"t{i}"] = "coding"
        engine._reconcile_cost(f"t{i}", "worker-1", 1)
    assert engine._default_cost_converter(_task(budget=100)) >= 1
