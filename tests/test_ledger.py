"""Tests for UnifiedLedger."""

import tempfile
from pathlib import Path

import pytest

from sovereign_os.ledger.unified_ledger import UnifiedLedger


def test_ledger_record_usd_and_balance():
    led = UnifiedLedger()
    assert led.total_usd_cents() == 0
    led.record_usd(1000)
    assert led.total_usd_cents() == 1000
    led.record_usd(-50, purpose="spend", ref="task-1")
    assert led.total_usd_cents() == 950


def test_ledger_record_token():
    led = UnifiedLedger()
    led.record_token("gpt-4o", input_tokens=100, output_tokens=50, task_id="t1")
    by_model = led.total_tokens_by_model()
    assert by_model.get("gpt-4o") == 150


def test_ledger_persist_and_reload():
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "ledger.jsonl"
        led1 = UnifiedLedger(persist_path=path)
        led1.record_usd(500)
        led1.record_usd(-100, ref="r1")
        assert led1.total_usd_cents() == 400
        led2 = UnifiedLedger(persist_path=path)
        assert led2.total_usd_cents() == 400
        assert len(led2.entries()) == 2


def test_ledger_runway_days():
    led = UnifiedLedger()
    led.record_usd(1000)
    assert led.runway_days(100) == 10
    assert led.runway_days(0) is None
