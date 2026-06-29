"""
submit_pr connector — the last step of a coding deliverable: branch, commit, push,
and open a PR with the fix. This is what turns a coding worker's diagnosis into a
real submission (the path to genuine bug-fix bounty delivery).

Outward-facing and write-heavy, so it is DRY-RUN by default and only runs when
SOVEREIGN_CODE_EXEC_ENABLED is truthy (or a `runner` is injected, for tests). Even
when enabled it runs git/gh under a timeout in the workspace dir. Requires `git`
(and `gh` for PR creation) on PATH.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

_URL = re.compile(r"https?://\S+")


def _subprocess_runner(cmd: list[str], cwd: str, timeout: float = 120.0) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr)
    except subprocess.TimeoutExpired:
        return -1, f"timed out after {timeout}s"
    except FileNotFoundError as e:
        return -1, f"command not found: {e}"


def submit_pr(
    root: str,
    *,
    branch: str,
    title: str,
    body: str = "",
    base: str = "main",
    add: str = "-A",
    runner: Callable[[list[str], str], tuple[int, str]] | None = None,
) -> dict:
    """
    Open a PR with the working-tree changes. Returns
    {"submitted","dry_run"?,"branch","pr_url"?,"steps"/"output", ...}.
    DRY-RUN unless SOVEREIGN_CODE_EXEC_ENABLED (or a runner is injected).
    """
    branch = (branch or "").strip()
    title = (title or "").strip()
    if not branch or not title:
        return {"submitted": False, "error": "branch and title are required"}
    if not Path(root).is_dir():
        return {"submitted": False, "error": "root is not a directory"}

    steps = [
        ["git", "checkout", "-b", branch],
        ["git", "add", add],
        ["git", "commit", "-m", title] + (["-m", body] if body else []),
        ["git", "push", "-u", "origin", branch],
        ["gh", "pr", "create", "--title", title, "--body", body or title, "--base", base, "--head", branch],
    ]

    enabled = runner is not None or os.getenv("SOVEREIGN_CODE_EXEC_ENABLED", "").lower() in ("1", "true", "yes")
    if not enabled:
        logger.info("CONNECTOR submit_pr DRY-RUN: would open PR '%s' from %s (set SOVEREIGN_CODE_EXEC_ENABLED).", title, branch)
        return {"submitted": False, "dry_run": True, "branch": branch, "title": title,
                "steps": [" ".join(s) for s in steps]}

    run = runner or _subprocess_runner
    output: list[dict] = []
    for s in steps:
        rc, out = run(s, root)
        output.append({"cmd": " ".join(s), "rc": rc, "out": (out or "")[-500:]})
        if rc != 0:
            logger.warning("CONNECTOR submit_pr step failed: %s (rc=%s)", " ".join(s), rc)
            return {"submitted": False, "branch": branch, "failed_at": " ".join(s), "rc": rc, "output": output}
    m = _URL.search(output[-1]["out"])
    return {"submitted": True, "branch": branch, "pr_url": m.group(0) if m else "", "output": output}
