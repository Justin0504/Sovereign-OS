"""Pytest fixtures for Sovereign-OS tests."""

import pytest

from sovereign_os.agents.auth import SovereignAuth
from sovereign_os.auditor import ReviewEngine
from sovereign_os.ledger.unified_ledger import UnifiedLedger
from sovereign_os.models.charter import (
    Charter,
    CoreCompetency,
    FiscalBoundaries,
    SuccessKPI,
)


@pytest.fixture
def charter():
    """Minimal charter for tests (no file I/O)."""
    return Charter(
        mission="Test mission: research and deliver.",
        core_competencies=[
            CoreCompetency(name="research", description="Research tasks", priority=8),
            CoreCompetency(name="code", description="Code tasks", priority=9),
        ],
        fiscal_boundaries=FiscalBoundaries(
            daily_burn_max_usd=50.0,
            max_budget_usd=2000.0,
            currency="USD",
        ),
        success_kpis=[
            SuccessKPI(
                name="task_ok",
                metric="tasks_verified_ok",
                target_value=0.95,
                unit="ratio",
                verification_prompt="Did the output satisfy the task?",
            ),
        ],
    )


@pytest.fixture
def ledger():
    """Fresh in-memory ledger with initial balance."""
    led = UnifiedLedger()
    led.record_usd(1000)
    return led


@pytest.fixture
def auth():
    return SovereignAuth()


@pytest.fixture
def review_engine(charter):
    """ReviewEngine with StubAuditor (no LLM calls in tests)."""
    from sovereign_os.auditor.review_engine import StubAuditor
    return ReviewEngine(charter, judge=StubAuditor())
