"""Background outbound poller: one tick settles a funded escrow; opt-in gating."""

import os
import pytest

from sovereign_os.auditor import ReviewEngine
from sovereign_os.auditor.review_engine import StubAuditor
from sovereign_os.agents.auth import SovereignAuth
from sovereign_os.governance.treasury import Treasury
from sovereign_os.ledger.unified_ledger import UnifiedLedger
from sovereign_os.models.charter import Charter
from sovereign_os.oversight import (
    OversightBroker, OversightRegistry, RentAHumanClient,
    start_oversight_poller, tick_once,
)


def _broker():
    led = UnifiedLedger(); led.record_usd(10000)
    reg = OversightRegistry()
    charter = Charter(mission="m")
    broker = OversightBroker(Treasury(charter, led), ReviewEngine(charter, judge=StubAuditor()),
                             RentAHumanClient("", live=False), ledger=led, registry=reg)
    return broker, reg


def test_tick_once_settles_funded_escrow():
    broker, reg = _broker()
    broker.post_governed_task(title="Gig", description="d", price_cents=2000)
    settled = tick_once(broker, reg)   # dry-run client reports 'delivered' -> settles
    assert len(settled) == 1 and settled[0]["action"] == "released"
    assert reg.summary().get("released") == 1


def test_poller_disabled_by_default(monkeypatch):
    monkeypatch.delenv("SOVEREIGN_OVERSIGHT_POLL_ENABLED", raising=False)
    broker, reg = _broker()
    assert start_oversight_poller(broker, reg) is None  # opt-in only
