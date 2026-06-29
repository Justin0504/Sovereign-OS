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


MAX_WRITE_BYTES = 1_000_000


def write_file(root: str | Path, relpath: str, content: str) -> dict:
    """
    Write a file under `root` (the agent applying its fix). Path-escape guarded.
    DRY-RUN unless SOVEREIGN_CODE_EXEC_ENABLED (writing to the repo is opt-in,
    same gate as run_tests). Returns {"written", "path", "bytes"|"dry_run", ...}.
    """
    target = _safe_path(root, relpath)
    if target is None:
        return {"written": False, "error": "path escapes workspace root (refused)"}
    content = content or ""
    if len(content.encode("utf-8", "replace")) > MAX_WRITE_BYTES:
        return {"written": False, "error": "content too large"}
    if os.getenv("SOVEREIGN_CODE_EXEC_ENABLED", "").lower() not in ("1", "true", "yes"):
        logger.info("CONNECTOR write_file DRY-RUN: would write %s (%d chars).", relpath, len(content))
        return {"written": False, "dry_run": True, "path": relpath}
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return {"written": True, "path": relpath, "bytes": len(content)}
    except Exception as e:
        return {"written": False, "error": str(e), "path": relpath}


def run_tests(root: str | Path, cmd: list[str] | None = None, *, timeout: float = 120.0, runner=None) -> dict:
    """
    Run a test command in `root`. DRY-RUN unless SOVEREIGN_CODE_EXEC_ENABLED is
    truthy. When SOVEREIGN_CODE_SANDBOX is set, runs inside Docker (no network,
    capped) — and REFUSES on the host if Docker is unavailable. Returns
    {"ran", "rc", "output", "passed", "sandboxed", ...}.
    """
    cmd = cmd or ["pytest", "-q"]
    if os.getenv("SOVEREIGN_CODE_EXEC_ENABLED", "").lower() not in ("1", "true", "yes"):
        logger.info("CONNECTOR run_tests DRY-RUN: would run %s in %s (set SOVEREIGN_CODE_EXEC_ENABLED).", cmd, root)
        return {"ran": False, "dry_run": True, "cmd": cmd}
    base = Path(root).resolve()
    if not base.is_dir():
        return {"ran": False, "error": "root is not a directory"}
    from sovereign_os.connectors.sandbox import sandbox_requested, select_test_runner

    run = runner or select_test_runner()
    sandboxed = runner is None and sandbox_requested()
    try:
        rc, out = run(cmd, str(base), timeout)
        return {"ran": True, "rc": rc, "output": (out or "")[-8000:], "passed": rc == 0, "sandboxed": sandboxed}
    except Exception as e:
        return {"ran": False, "error": str(e), "sandboxed": sandboxed}
