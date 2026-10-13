"""Pytest fixtures for Sovereign-OS tests."""

import os

import pytest

# Provider credentials the code reads from the environment. Every one of these is a
# path out of the process to a paid, rate-limited, non-deterministic third party.
_PROVIDER_KEYS = (
    # models
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "IMAGE_GEN_API_KEY",
    # money
    "STRIPE_API_KEY",
    "X402_API_KEY",
    # marketplaces and human work
    "RENTAHUMAN_API_KEY",
    "CLAWTASKS_API_KEY",
    "APB_API_KEY",
    "BOTBOUNTY_API_KEY",
    "TASKBOUNTY_API_KEY",
    "BRIDGE_RETAIL_API_KEY",
    # the ingest bridge's credential for calling a Sovereign-OS instance: outbound from
    # the bridge process, unlike SOVEREIGN_API_KEY which guards this system's own inbound
    # requests and is left alone.
    "SOVEREIGN_OS_API_KEY",
)


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "live_api: test deliberately calls a real provider; requires --live-api to run.",
    )


def pytest_addoption(parser):
    parser.addoption(
        "--live-api", action="store_true", default=False,
        help="Permit tests marked `live_api` to reach real providers. Costs money.",
    )


@pytest.fixture(autouse=True)
def no_live_api(request, monkeypatch):
    """
    Keep the suite off the network, whatever happens to be in the developer's shell.

    The code falls back to `os.getenv("OPENAI_API_KEY")` when no client is injected, so
    a test that means to exercise the stub path silently exercises the live API instead
    the moment a key is exported — which is the normal state of a working machine. The
    tests then pass or fail on someone's billing status rather than on the code: this
    suite reported 657 passed with no key set and 639 passed / 18 failed with an
    exhausted one, same commit, and every failure was HTTP 429.

    Scrubbing the variables here makes the default hermetic rather than ambient. A test
    that genuinely wants a provider marks itself `live_api` and runs under --live-api.
    """
    if request.node.get_closest_marker("live_api"):
        if not request.config.getoption("--live-api"):
            pytest.skip("needs --live-api (makes real, billable provider calls)")
        return
    for name in _PROVIDER_KEYS:
        monkeypatch.delenv(name, raising=False)
    # Belt and braces: a client constructed from a default-argument key captured at
    # import time would not see the delenv above, so make the absence unmistakable.
    monkeypatch.setenv("SOVEREIGN_OFFLINE_TESTS", "1")
    assert not any(os.getenv(k) for k in _PROVIDER_KEYS)

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
