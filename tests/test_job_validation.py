"""Unit tests for job input validation (no TestClient)."""

import pytest

from sovereign_os.web.app import (
    JOB_AMOUNT_CENTS_MAX,
    JOB_AMOUNT_CENTS_MIN,
    JOB_GOAL_MAX_LEN,
    _secure_compare,
    validate_job_input,
)


def test_validate_job_input_goal_too_long():
    validate_job_input("ok", 0, None)
    with pytest.raises(ValueError, match="goal length exceeds"):
        validate_job_input("x" * (JOB_GOAL_MAX_LEN + 1), 0, None)


def test_validate_job_input_amount_bounds():
    validate_job_input("ok", JOB_AMOUNT_CENTS_MIN, None)
    validate_job_input("ok", JOB_AMOUNT_CENTS_MAX, None)
    with pytest.raises(ValueError, match="amount_cents"):
        validate_job_input("ok", JOB_AMOUNT_CENTS_MIN - 1, None)
    with pytest.raises(ValueError, match="amount_cents"):
        validate_job_input("ok", JOB_AMOUNT_CENTS_MAX + 1, None)


def test_validate_job_input_callback_url_invalid():
    validate_job_input("ok", 0, None)
    validate_job_input("ok", 0, "https://example.com/cb")
    validate_job_input("ok", 0, "http://host/path")
    with pytest.raises(ValueError, match="callback_url"):
        validate_job_input("ok", 0, "not-a-url")
    with pytest.raises(ValueError, match="callback_url"):
        validate_job_input("ok", 0, "ftp://host/path")


def test_validate_job_input_callback_url_ssrf_rejected():
    """callback_url to localhost or private IP is rejected (SSRF protection)."""
    with pytest.raises(ValueError, match="localhost|SSRF|private"):
        validate_job_input("ok", 0, "http://localhost/cb")
    with pytest.raises(ValueError, match="localhost|SSRF|private"):
        validate_job_input("ok", 0, "https://127.0.0.1/hook")
    with pytest.raises(ValueError, match="SSRF|private"):
        validate_job_input("ok", 0, "http://192.168.1.1/cb")
    with pytest.raises(ValueError, match="SSRF|private"):
        validate_job_input("ok", 0, "http://10.0.0.1/cb")
    validate_job_input("ok", 0, "https://example.com/cb", ssrf_check=True)
    validate_job_input("ok", 0, "http://localhost/cb", ssrf_check=False)  # opt-out for tests


def test_validate_job_input_custom_bounds():
    validate_job_input("ab", 5, None, goal_max_len=2, amount_min=0, amount_max=10)
    with pytest.raises(ValueError, match="goal length"):
        validate_job_input("abc", 5, None, goal_max_len=2)
    with pytest.raises(ValueError, match="amount_cents"):
        validate_job_input("ok", 11, None, amount_min=0, amount_max=10)


def test_secure_compare_constant_time():
    """API key comparison is constant-time to avoid timing attacks."""
    assert _secure_compare("same", "same") is True
    assert _secure_compare("a", "b") is False
    assert _secure_compare("", "") is True
    assert _secure_compare("key", "key ") is False
