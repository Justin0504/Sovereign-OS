"""
Tool sets that workers expose to their tool-use loop (BaseWorker.run_with_tools).
Each tool is a callable(args: dict) -> short observation string, backed by a real
connector. Safety-sensitive args (file root) are pinned by the worker, not the model.
"""

from __future__ import annotations

from typing import Any, Callable


def web_tools() -> tuple[dict[str, Callable[[dict], str]], dict[str, str]]:
    """web_fetch: fetch a URL and return readable text (for research/data)."""
    from sovereign_os.connectors import dispatch

    def _fetch(args: dict) -> str:
        r = dispatch("web_fetch", url=str(args.get("url", "")))
        if r.get("error"):
            return f"error: {r['error']}"
        return f"[{r.get('status')}] {r.get('text', '')[:3000]}"

    return ({"web_fetch": _fetch}, {"web_fetch": "Fetch a URL -> readable text. args: {url}"})


def code_workspace_tools(root: str) -> tuple[dict[str, Callable[[dict], str]], dict[str, str]]:
    """
    Read-only repo tools rooted at `root` (the model cannot change the root):
    list_files, read_file, and run_tests (run_tests stays dry-run unless
    SOVEREIGN_CODE_EXEC_ENABLED). For coding workers.
    """
    from sovereign_os.connectors import dispatch

    def _list(args: dict) -> str:
        r = dispatch("list_files", root=root, glob=str(args.get("glob", "**/*")))
        return "files: " + ", ".join(r.get("files", [])[:80])

    def _read(args: dict) -> str:
        r = dispatch("read_file", root=root, relpath=str(args.get("relpath", "")))
        return r.get("error") and f"error: {r['error']}" or r.get("text", "")[:3000]

    def _tests(args: dict) -> str:
        r = dispatch("run_tests", root=root, action="run_tests", cmd=args.get("cmd"))
        if r.get("dry_run"):
            return "run_tests is disabled (dry-run); set SOVEREIGN_CODE_EXEC_ENABLED to run."
        return f"rc={r.get('rc')} passed={r.get('passed')}\n{r.get('output', '')[:2000]}"

    handlers: dict[str, Callable[[dict], str]] = {"list_files": _list, "read_file": _read, "run_tests": _tests}
    descs = {
        "list_files": "List files in the repo. args: {glob?}",
        "read_file": "Read a file. args: {relpath}",
        "run_tests": "Run the test suite (guarded). args: {cmd?}",
    }
    return handlers, descs


def use_tools_enabled(ctx: dict[str, Any] | None) -> bool:
    """Tool-use is opt-in via context['use_tools'] (keeps default single-shot behavior)."""
    try:
        return str((ctx or {}).get("use_tools", "")).lower() in ("1", "true", "yes", "auto")
    except Exception:
        return False
