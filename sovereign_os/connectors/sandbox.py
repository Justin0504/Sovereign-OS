"""
Sandboxed execution for untrusted code (the test runner). Real bug-fix bounties
mean running a stranger's repo, so `code_workspace.run_tests` can execute inside a
Docker container — no network, capped memory/CPU, under a timeout — instead of
raw subprocess.

Selection (env):
  SOVEREIGN_CODE_SANDBOX=docker   -> run tests in a container
  SOVEREIGN_SANDBOX_IMAGE         -> image to use (default python:3.12-slim)
  SOVEREIGN_SANDBOX_NETWORK       -> "none" (default) or "bridge"

When the sandbox is requested but Docker is unavailable, the runner REFUSES
(returns a nonzero result) rather than silently running untrusted code on the host.
`submit_pr` is NOT sandboxed — it is the operator's own git/credentials (trusted).
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess

logger = logging.getLogger(__name__)

DEFAULT_IMAGE = "python:3.12-slim"


def sandbox_requested() -> bool:
    return os.getenv("SOVEREIGN_CODE_SANDBOX", "").strip().lower() in ("1", "true", "yes", "docker")


def sandbox_available() -> bool:
    return shutil.which("docker") is not None


def build_docker_cmd(
    cmd: list[str],
    cwd: str,
    *,
    image: str = DEFAULT_IMAGE,
    network: str = "none",
    memory: str = "512m",
    cpus: str = "1",
) -> list[str]:
    """Build the `docker run` argv that executes `cmd` against the mounted workspace."""
    inner = " ".join(_shquote(c) for c in cmd)
    return [
        "docker", "run", "--rm",
        f"--network={network}",
        f"--memory={memory}", f"--cpus={cpus}",
        "--pids-limit=512",
        "-v", f"{cwd}:/work", "-w", "/work",
        image, "sh", "-lc", inner,
    ]


def _shquote(s: str) -> str:
    import shlex
    return shlex.quote(s)


def _subprocess_runner(cmd: list[str], cwd: str, timeout: float = 120.0) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr)
    except subprocess.TimeoutExpired:
        return -1, f"timed out after {timeout}s"
    except FileNotFoundError as e:
        return -1, f"command not found: {e}"


def _docker_runner(cmd: list[str], cwd: str, timeout: float = 120.0) -> tuple[int, str]:
    image = os.getenv("SOVEREIGN_SANDBOX_IMAGE", DEFAULT_IMAGE)
    network = os.getenv("SOVEREIGN_SANDBOX_NETWORK", "none")
    dcmd = build_docker_cmd(cmd, cwd, image=image, network=network)
    return _subprocess_runner(dcmd, cwd, timeout=timeout)


def select_test_runner():
    """
    Pick the runner for executing repo tests:
    - sandbox requested + Docker present -> container runner
    - sandbox requested + Docker absent  -> refusing runner (won't run on host)
    - otherwise                          -> plain subprocess
    """
    if sandbox_requested():
        if sandbox_available():
            return _docker_runner
        logger.warning("SANDBOX requested but Docker is unavailable; refusing to run untrusted code on the host.")
        return lambda cmd, cwd, timeout=120.0: (-1, "sandbox requested but Docker unavailable; refused")
    return _subprocess_runner
