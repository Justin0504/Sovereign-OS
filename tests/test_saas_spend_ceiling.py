"""
A plan's daily spend cap has to bound what a mission spends, not only when one may start.

`can_run` is a pre-flight check: it compares today's total against the plan cap before a
mission begins. The in-flight bound is the SpendCircuitBreaker, and its ceiling was set to
the whole plan cap while a fresh breaker is built per mission. So a tenant 450c into a
500c day began the next mission with a 500c ceiling and could reach 950c -- and the free
plan allows twenty missions a day.

That is a pre-flight gate with no in-flight bound, which is the failure this system was
built to catch, occurring on the platform's own billing boundary.
"""

import tempfile

import pytest

from sovereign_os.saas.plans import PLANS, get_plan
from sovereign_os.saas.runtime import build_tenant_engine
from sovereign_os.saas.tenancy import TenantStore


def _tenant(plan="free", spent_today=0):
    store = TenantStore(tempfile.mkdtemp())
    t = store.create("Acme", plan=plan)
    t.config.anthropic_api_key = "sk-ant-api03-" + "x" * 32
    store.update(t)
    if spent_today:
        store.record_mission(t.id, spend_cents=spent_today)
    return t, store


def _ceiling(t, store):
    engine, _ledger, _auth = build_tenant_engine(t, store)
    return engine._circuit_breaker.session_ceiling_cents


def test_a_fresh_day_gets_the_whole_plan_cap():
    t, store = _tenant("free")
    assert _ceiling(t, store) == get_plan("free").max_daily_spend_cents


def test_the_ceiling_is_what_is_left_not_what_the_plan_allows():
    cap = get_plan("free").max_daily_spend_cents
    t, store = _tenant("free", spent_today=cap - 50)
    assert _ceiling(t, store) == 50, (
        "a mission may spend only the tenant's remaining daily allowance")


@pytest.mark.parametrize("spent_fraction", [0.0, 0.5, 0.9, 0.99])
def test_the_ceiling_tracks_spend_across_the_day(spent_fraction):
    cap = get_plan("free").max_daily_spend_cents
    spent = int(cap * spent_fraction)
    t, store = _tenant("free", spent_today=spent)
    assert _ceiling(t, store) == max(1, cap - spent)


def test_an_overspent_tenant_is_not_handed_an_unlimited_session():
    """
    The trap in this fix: 0 means "no ceiling" to the breaker, so clamping with
    max(0, cap - spent) would turn the most overspent tenant into the least limited one.
    """
    cap = get_plan("free").max_daily_spend_cents
    t, store = _tenant("free", spent_today=cap * 3)
    ceiling = _ceiling(t, store)
    assert ceiling != 0, "0 would disable the breaker entirely"
    assert ceiling == 1


def test_the_daily_total_cannot_exceed_the_cap_by_more_than_one_mission_in_flight():
    """
    The property that matters, stated over the whole day rather than one mission: after
    any number of missions, the ceiling granted plus what has already been spent never
    exceeds the plan cap.
    """
    cap = get_plan("free").max_daily_spend_cents
    for spent in range(0, cap + 1, 37):
        t, store = _tenant("free", spent_today=spent)
        assert _ceiling(t, store) + spent <= cap or spent >= cap


@pytest.mark.parametrize("plan", sorted(PLANS))
def test_every_plan_is_bounded_by_its_own_cap(plan):
    cap = get_plan(plan).max_daily_spend_cents
    if not cap:
        pytest.skip(f"plan {plan} has no daily cap")
    t, store = _tenant(plan, spent_today=cap // 2)
    assert _ceiling(t, store) == cap - cap // 2


def test_a_plan_without_a_daily_cap_keeps_an_unbounded_session():
    """0 has to keep meaning "no cap", or adding an unlimited plan silently throttles it."""
    import dataclasses

    from sovereign_os.saas import plans as plans_mod

    unlimited = dataclasses.replace(get_plan("team"), name="unlimited",
                                    max_daily_spend_cents=0)
    plans_mod.PLANS["unlimited"] = unlimited
    try:
        t, store = _tenant("unlimited", spent_today=10_000)
        assert _ceiling(t, store) == 0
    finally:
        del plans_mod.PLANS["unlimited"]
