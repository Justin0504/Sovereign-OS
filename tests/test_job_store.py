"""Tests for SQLite JobStore."""

import pytest

from sovereign_os.jobs.store import JobStore, JobRow


def test_job_store_add_and_list():
    store = JobStore(":memory:")
    row = store.add_job("Goal A", "Charter1", amount_cents=100)
    assert row.job_id >= 1
    assert row.goal == "Goal A"
    assert row.status == "pending"
    jobs = store.list_jobs()
    assert len(jobs) == 1
    assert jobs[0].job_id == row.job_id


def test_job_store_update():
    store = JobStore(":memory:")
    store.add_job("G", "C")
    store.add_job("G2", "C")
    ok = store.update_job(1, status="approved")
    assert ok is True
    j = store.get_job(1)
    assert j is not None
    assert j.status == "approved"
    ok2 = store.update_job(1, status="completed", payment_id="pay_123")
    assert ok2 is True
    j2 = store.get_job(1)
    assert j2 is not None
    assert j2.status == "completed"
    assert j2.payment_id == "pay_123"


def test_job_store_get_missing():
    store = JobStore(":memory:")
    assert store.get_job(999) is None
