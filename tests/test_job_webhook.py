"""Tests for job completion webhook payload and signature."""

import json
from unittest.mock import patch, MagicMock

import pytest

from sovereign_os.web import job_webhook


def test_webhook_payload_has_required_keys():
    """Payload built by module includes job_id, status, goal, result_summary, audit_score, charter."""
    payload = job_webhook._build_payload(
        job_id=1,
        status="completed",
        goal="Summarize X",
        amount_cents=100,
        currency="USD",
        payment_id="ch_xxx",
        completed_at="2025-01-01T00:00:00Z",
        result_summary="Done.",
        audit_score=0.9,
        charter="Default",
    )
    assert payload["job_id"] == 1
    assert payload["status"] == "completed"
    assert payload["goal"] == "Summarize X"
    assert payload["result_summary"] == "Done."
    assert payload["audit_score"] == 0.9
    assert payload["charter"] == "Default"
    assert payload["payment_id"] == "ch_xxx"
    body = json.dumps(payload, ensure_ascii=False)
    assert "job_id" in body and "completed_at" in body


def test_webhook_payload_includes_request_id_when_provided():
    """When request_id is passed, payload contains request_id."""
    payload = job_webhook._build_payload(
        job_id=1,
        status="completed",
        goal="X",
        amount_cents=0,
        currency="USD",
        payment_id=None,
        completed_at="2025-01-01T00:00:00Z",
        result_summary="",
        audit_score=0.9,
        charter="Default",
        request_id="abc123",
    )
    assert payload.get("request_id") == "abc123"


def test_webhook_signature_header_when_secret_set():
    """When secret is provided, X-Sovereign-Signature is sha256=..."""
    sig = job_webhook._sign_payload(b'{"job_id":1}', "my-secret")
    assert sig.startswith("sha256=")
    assert len(sig) == 7 + 64  # "sha256=" + 64 hex chars


@patch("urllib.request.urlopen")
def test_notify_job_completion_posts_json(mock_urlopen):
    """notify_job_completion sends POST with JSON body to given URL."""
    resp = MagicMock()
    resp.getcode.return_value = 200
    mock_urlopen.return_value.__enter__ = lambda self: resp
    mock_urlopen.return_value.__exit__ = lambda *a: None

    job_webhook.notify_job_completion(
        webhook_url="http://example.com/hook",
        job_id=2,
        status="payment_failed",
        goal="Task",
        amount_cents=50,
        currency="USD",
        payment_id=None,
        completed_at="2025-01-01T12:00:00Z",
        result_summary="",
        audit_score=0.0,
        charter="Default",
        secret=None,
    )
    assert mock_urlopen.called
    call_args = mock_urlopen.call_args
    req = call_args[0][0]
    assert req.get_header("Content-type", "").startswith("application/json")
    assert b"job_id" in req.data
    assert b"payment_failed" in req.data


@patch("sovereign_os.web.job_webhook._post", side_effect=Exception("network error"))
@patch("sovereign_os.web.job_webhook._write_failure_log")
def test_webhook_failure_writes_log(mock_write_log, _mock_post):
    """When POST fails after all retries, _write_failure_log is called with url, payload, error."""
    job_webhook.notify_job_completion(
        webhook_url="http://example.com/hook",
        job_id=3,
        status="completed",
        goal="Test",
        amount_cents=100,
        currency="USD",
        payment_id=None,
        completed_at="2025-01-01T00:00:00Z",
        result_summary="",
        audit_score=0.9,
        charter="Default",
        secret=None,
    )
    mock_write_log.assert_called_once()
    args, kwargs = mock_write_log.call_args
    assert args[0] == "http://example.com/hook"
    assert args[1]["job_id"] == 3
    assert args[1]["status"] == "completed"
    assert "network error" in args[2]
