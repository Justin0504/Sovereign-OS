"""Tests for telemetry: job metrics and Prometheus output."""

import pytest

from sovereign_os.telemetry import tracer


def test_record_job_completed_no_crash():
    """record_job_completed and set_job_queue_gauges do not raise."""
    tracer.record_job_completed("completed", 1.5)
    tracer.record_job_completed("failed", 0.1)
    tracer.set_job_queue_gauges(3, 1)


def test_get_prometheus_metrics_output_returns_bytes():
    """get_prometheus_metrics_output returns bytes (with or without prometheus_client)."""
    out = tracer.get_prometheus_metrics_output(pending=2, running=0)
    assert isinstance(out, bytes)
    assert b"sovereign" in out or b"#" in out
