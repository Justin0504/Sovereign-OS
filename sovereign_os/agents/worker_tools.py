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

    def _write(args: dict) -> str:
        r = dispatch("write_file", root=root, relpath=str(args.get("relpath", "")), content=str(args.get("content", "")))
        if r.get("dry_run"):
            return "write_file is disabled (dry-run); set SOVEREIGN_CODE_EXEC_ENABLED to apply the fix."
        if r.get("error") or not r.get("written"):
            return f"not written: {r.get('error', 'unknown')}"
        return f"wrote {r.get('path')} ({r.get('bytes')} bytes)"

    def _tests(args: dict) -> str:
        r = dispatch("run_tests", root=root, action="run_tests", cmd=args.get("cmd"))
        if r.get("dry_run"):
            return "run_tests is disabled (dry-run); set SOVEREIGN_CODE_EXEC_ENABLED to run."
        return f"rc={r.get('rc')} passed={r.get('passed')}\n{r.get('output', '')[:2000]}"

    def _pr(args: dict) -> str:
        r = dispatch("submit_pr", root=root, branch=str(args.get("branch", "")),
                     title=str(args.get("title", "")), body=str(args.get("body", "")),
                     base=str(args.get("base", "main")))
        if r.get("dry_run"):
            return "submit_pr is disabled (dry-run); set SOVEREIGN_CODE_EXEC_ENABLED to open a PR."
        if r.get("error") or not r.get("submitted"):
            return f"PR not submitted: {r.get('error') or r.get('failed_at', 'unknown')}"
        return f"PR opened: {r.get('pr_url') or '(no url)'}"

    handlers: dict[str, Callable[[dict], str]] = {
        "list_files": _list, "read_file": _read, "write_file": _write, "run_tests": _tests, "submit_pr": _pr,
    }
    descs = {
        "list_files": "List files in the repo. args: {glob?}",
        "read_file": "Read a file. args: {relpath}",
        "write_file": "Write/overwrite a file with your fix (guarded). args: {relpath, content}",
        "run_tests": "Run the test suite (guarded). args: {cmd?}",
        "submit_pr": "Open a PR with your changes (guarded). args: {branch, title, body?}",
    }
    return handlers, descs


def figma_tools() -> tuple[dict[str, Callable[[dict], str]], dict[str, str]]:
    """read_figma: read a referenced Figma file's structure (for design tasks)."""
    from sovereign_os.connectors import dispatch

    def _read(args: dict) -> str:
        r = dispatch("figma", ref=str(args.get("ref", "") or args.get("url", "")))
        if r.get("error"):
            return f"error: {r['error']}"
        return f"Figma '{r.get('name', '')}':\n{r.get('summary', '')[:3000]}"

    return ({"read_figma": _read}, {"read_figma": "Read a Figma file's structure. args: {ref (URL or key)}"})


def image_tools() -> tuple[dict[str, Callable[[dict], str]], dict[str, str]]:
    """generate_image: render a visual from a prompt (dry-run unless configured)."""
    from sovereign_os.connectors import dispatch

    def _gen(args: dict) -> str:
        r = dispatch("image_gen", prompt=str(args.get("prompt", "")), size=str(args.get("size", "1024x1024")))
        if r.get("error"):
            return f"error: {r['error']}"
        if r.get("dry_run"):
            return "image_gen is not configured (dry-run); set IMAGE_GEN_API_KEY to render."
        return f"image generated: {r.get('url') or '(base64 data)'}"

    return ({"generate_image": _gen}, {"generate_image": "Render an image from a prompt. args: {prompt, size?}"})


def use_tools_enabled(ctx: dict[str, Any] | None) -> bool:
    """Tool-use is opt-in via context['use_tools'] (keeps default single-shot behavior)."""
    try:
        return str((ctx or {}).get("use_tools", "")).lower() in ("1", "true", "yes", "auto")
    except Exception:
        return False


# Categories whose deliverables benefit from real tools (web/repo/design access).
TOOL_CATEGORIES = {"research", "data", "coding", "design"}


def auto_tool_context(skill: str) -> dict[str, str]:
    """
    Context additions to auto-enable the worker tool loop for tool-benefiting
    categories. Opt-in globally via SOVEREIGN_AUTO_TOOLS (default off = unchanged).
    Injects workspace_root (SOVEREIGN_WORKSPACE_ROOT) for coding tasks.
    """
    import os

    if os.getenv("SOVEREIGN_AUTO_TOOLS", "").lower() not in ("1", "true", "yes"):
        return {}
    from sovereign_os.agents.categories import category_for_skill

    cat = category_for_skill(skill).key
    if cat not in TOOL_CATEGORIES:
        return {}
    out = {"use_tools": "1"}
    root = os.getenv("SOVEREIGN_WORKSPACE_ROOT")
    if root and cat == "coding":
        out["workspace_root"] = root
    return out
