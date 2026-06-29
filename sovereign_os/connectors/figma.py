"""
figma connector — READ a Figma design file so a worker can implement/extend/audit
it (names, structure, components, key styles). Uses the Figma REST API with
FIGMA_TOKEN.

Scope/limitation: Figma's REST API is read-only — agents can read a file's
structure but cannot create or edit designs via REST (authoring needs Figma's
in-app Plugin/Agent). So this grounds design work in an existing file; it does
not render new designs. `opener(url, token, timeout)` is injectable for tests.
"""

from __future__ import annotations

import json
import logging
import os
import re

logger = logging.getLogger(__name__)

API = "https://api.figma.com/v1"


def is_configured() -> bool:
    return bool(os.getenv("FIGMA_TOKEN"))


def file_key_from(ref: str) -> str:
    """Extract a file key from a Figma URL, or return ref unchanged if already a key."""
    m = re.search(r"figma\.com/(?:file|design)/([A-Za-z0-9_-]+)", ref or "")
    return m.group(1) if m else (ref or "").strip()


def summarize_document(node: dict, *, max_nodes: int = 150, max_depth: int = 5) -> str:
    """Render a Figma document tree as an indented name (TYPE) outline, capped."""
    lines: list[str] = []
    count = 0

    def walk(n: dict, depth: int) -> None:
        nonlocal count
        if count >= max_nodes or depth > max_depth:
            return
        name = str(n.get("name", "")).strip()
        ntype = n.get("type", "")
        if name or ntype:
            lines.append(f"{'  ' * depth}- {name} ({ntype})")
            count += 1
        for child in (n.get("children") or []):
            if count >= max_nodes:
                break
            walk(child, depth + 1)

    walk(node, 0)
    out = "\n".join(lines)
    if count >= max_nodes:
        out += "\n  … (truncated)"
    return out


def _get_json(url: str, token: str, opener, timeout: float):
    if opener is not None:
        resp = opener(url, token, timeout)
    else:
        import urllib.request
        req = urllib.request.Request(url, headers={"X-Figma-Token": token})
        resp = urllib.request.urlopen(req, timeout=timeout)  # noqa: S310 - fixed api host
    raw = resp.read()
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    return json.loads(raw)


def figma_get_file(ref: str, *, token: str | None = None, opener=None, timeout: float = 15.0) -> dict:
    """
    Read a Figma file (by URL or key) and return {"file_key","name","summary"} —
    an outline of the document tree the worker can design against. opener bypasses
    the token check for tests.
    """
    token = token or os.getenv("FIGMA_TOKEN", "")
    if not token and opener is None:
        return {"error": "FIGMA_TOKEN not set; cannot read Figma file."}
    key = file_key_from(ref)
    if not key:
        return {"error": "no Figma file key/URL provided"}
    try:
        data = _get_json(f"{API}/files/{key}", token, opener, timeout)
        return {
            "file_key": key,
            "name": data.get("name", ""),
            "summary": summarize_document(data.get("document", {})),
        }
    except Exception as e:
        logger.warning("CONNECTOR figma_get_file failed for %s: %s", key, e)
        return {"error": str(e), "file_key": key}
