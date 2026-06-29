"""
code_workspace connector — read-only access to a code checkout, plus a guarded
test runner. Gives coding workers the context that TaskBounty-style bug-fix
bounties require (read the repo, then propose a fix).

Safety:
- list_files / read_file refuse any path that escapes `root` (resolved + containment
  check), and cap read size.
- run_tests executes a command only when SOVEREIGN_CODE_EXEC_ENABLED is truthy;
  otherwise it is a dry-run no-op. Even when enabled it runs under a timeout in
  the workspace dir. Arbitrary code execution is opt-in and the operator's call.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

MAX_READ_BYTES = 200_000
MAX_LIST = 2000


def _safe_path(root: str | Path, relpath: str) -> Path | None:
    """Resolve relpath under root; return None if it escapes root."""
    base = Path(root).resolve()
    target = (base / relpath).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        return None
    return target


def list_files(root: str | Path, glob: str = "**/*") -> dict:
    base = Path(root).resolve()
    if not base.is_dir():
        return {"root": str(base), "files": [], "error": "root is not a directory"}
    files = []
    for p in base.glob(glob):
        if p.is_file():
            files.append(str(p.relative_to(base)))
            if len(files) >= MAX_LIST:
                break
    return {"root": str(base), "files": sorted(files), "truncated": len(files) >= MAX_LIST}


def read_file(root: str | Path, relpath: str) -> dict:
    target = _safe_path(root, relpath)
    if target is None:
        return {"path": relpath, "text": "", "error": "path escapes workspace root (refused)"}
    if not target.is_file():
        return {"path": relpath, "text": "", "error": "not a file"}
    try:
        data = target.read_bytes()[: MAX_READ_BYTES + 1]
        truncated = len(data) > MAX_READ_BYTES
        return {"path": relpath, "text": data[:MAX_READ_BYTES].decode("utf-8", "replace"), "truncated": truncated}
    except Exception as e:
        return {"path": relpath, "text": "", "error": str(e)}


def run_tests(root: str | Path, cmd: list[str] | None = None, *, timeout: float = 120.0) -> dict:
    """
    Run a test command in `root`. DRY-RUN unless SOVEREIGN_CODE_EXEC_ENABLED is
    truthy (arbitrary execution is opt-in). Returns {"ran", "rc", "output", ...}.
    """
    cmd = cmd or ["pytest", "-q"]
    if os.getenv("SOVEREIGN_CODE_EXEC_ENABLED", "").lower() not in ("1", "true", "yes"):
        logger.info("CONNECTOR run_tests DRY-RUN: would run %s in %s (set SOVEREIGN_CODE_EXEC_ENABLED).", cmd, root)
        return {"ran": False, "dry_run": True, "cmd": cmd}
    base = Path(root).resolve()
    if not base.is_dir():
        return {"ran": False, "error": "root is not a directory"}
    try:
        proc = subprocess.run(cmd, cwd=str(base), capture_output=True, text=True, timeout=timeout)
        out = (proc.stdout + proc.stderr)[-8000:]
        return {"ran": True, "rc": proc.returncode, "output": out, "passed": proc.returncode == 0}
    except subprocess.TimeoutExpired:
        return {"ran": True, "rc": -1, "error": f"timed out after {timeout}s"}
    except Exception as e:
        return {"ran": False, "error": str(e)}
