"""
Tests that the test suite itself does not reach a paid provider.

This exists because the suite was ambient rather than hermetic: the LLM clients fall back
to `os.getenv("OPENAI_API_KEY")` when nothing is injected, so 18 tests that were written
to exercise a stub quietly exercised the live API instead whenever a key happened to be
exported — which is the normal state of a working machine. Same commit, two outcomes:

    key unset     657 passed in 15s
    key exhausted 639 passed, 18 failed in 53s     (every failure HTTP 429)

The tests were therefore passing or failing on someone's billing status. The autouse
fixture in `conftest.py` scrubs the variables; these tests pin that it is still wired,
because a guard that silently stops being autouse looks exactly like a guard that works.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import _PROVIDER_KEYS

REPO = Path(__file__).resolve().parent.parent


def test_no_provider_credential_is_visible_to_a_test():
    """The fixture is autouse, so this holds without the test asking for anything."""
    visible = [k for k in _PROVIDER_KEYS if os.getenv(k)]
    assert visible == [], f"a test can reach a paid provider through {visible}"


def test_the_scrub_covers_every_key_the_code_actually_reads():
    """
    A list of names drifts from the code it is meant to cover. Anything the package reads
    from the environment and calls an API key has to be in the scrub list, or a new
    provider reintroduces the problem silently.
    """
    read_by_code = set()
    for path in (REPO / "sovereign_os").rglob("*.py"):
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            if "getenv" not in line and "environ" not in line:
                continue
            for token in line.replace('"', " ").replace("'", " ").split():
                if token.endswith("_API_KEY"):
                    read_by_code.add(token)

    # SOVEREIGN_API_KEY is this system's own inbound auth, not an outbound credential.
    read_by_code.discard("SOVEREIGN_API_KEY")
    missing = sorted(read_by_code - set(_PROVIDER_KEYS))
    assert not missing, (
        f"these outbound credentials are read by the code but not scrubbed in "
        f"conftest: {missing}"
    )


@pytest.mark.live_api
def test_a_live_marked_test_is_skipped_by_default():
    """Reaching this body without --live-api means the opt-in gate is not working."""
    raise AssertionError("a live_api test ran without --live-api")


def test_the_previously_live_tests_pass_with_a_bogus_key_exported():
    """
    The load-bearing one, and the reason this file drives a subprocess rather than
    asserting on a fixture: it reproduces the exact condition that broke the suite — a
    syntactically valid but useless key in the environment — against the real tests that
    failed under it. A key that is never used cannot return 429.
    """
    env = dict(os.environ)
    env["OPENAI_API_KEY"] = "sk-bogus-must-never-be-used"
    env["ANTHROPIC_API_KEY"] = "sk-ant-bogus-must-never-be-used"
    env["PYTHONPATH"] = str(REPO)

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "tests/test_multitask_decomp.py", "tests/test_governance_engine.py"],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=300,
    )
    assert result.returncode == 0, (
        "tests that are supposed to use the stub path reached a provider:\n"
        + result.stdout[-3000:]
    )
    assert "RateLimitError" not in result.stdout
