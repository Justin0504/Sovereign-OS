"""
Go-live safety check for ClawTasks. Run this BEFORE setting CLAWTASKS_LIVE=true.

It verifies configuration and the claim/submit code path WITHOUT moving any funds,
and returns GO / NO-GO. Blockers (missing key when live, unreachable API) fail the
check; warnings (free-tasks-only mode, live flag set) are surfaced but don't block.

  python -m sovereign_os.ingest_bridge.clawtasks_preflight
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable

from sovereign_os.ingest_bridge.sources.clawtasks import (
    DEFAULT_BASE_URL,
    ClawTasksClient,
    _http_get_json,
)

logger = logging.getLogger(__name__)

OK, WARN, BLOCK = "ok", "warn", "blocker"


def run_preflight(
    *,
    api_key: str | None = None,
    live: bool | None = None,
    base_url: str | None = None,
    get_json: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """
    Return {"go": bool, "live": bool, "checks": [{"name","status","detail"}]}.

    Never claims or submits for real — the claim/submit verification uses a
    dry-run client. `get_json` is injectable for tests.
    """
    api_key = api_key if api_key is not None else os.getenv("CLAWTASKS_API_KEY", "")
    live = live if live is not None else os.getenv("CLAWTASKS_LIVE", "").lower() in ("1", "true", "yes")
    base_url = (base_url or os.getenv("CLAWTASKS_BASE_URL", DEFAULT_BASE_URL)).rstrip("/")
    get_json = get_json or _http_get_json

    checks: list[dict[str, str]] = []

    def add(name: str, status: str, detail: str) -> None:
        checks.append({"name": name, "status": status, "detail": detail})

    # 1) Mode banner
    add("mode", WARN if live else OK, "LIVE — will stake real USDC" if live else "dry-run (no funds move)")

    # 2) API key presence (blocker only when live)
    if api_key:
        add("api_key", OK, "present")
    else:
        add("api_key", BLOCK if live else WARN, "CLAWTASKS_API_KEY not set (required for live claim/submit)")

    # 3) /config reachability + parse
    cfg: dict[str, Any] = {}
    try:
        cfg = get_json(f"{base_url}/config", {}, {}, 12.0) or {}
        add("config_reachable", OK, f"chain_id={cfg.get('chain_id')} stake_percent={cfg.get('stake_percent')} min_bounty={cfg.get('min_bounty')}")
    except Exception as e:
        add("config_reachable", BLOCK if live else WARN, f"GET /config failed: {e}")

    # 4) free-tasks-only mode
    if cfg:
        if cfg.get("free_tasks_only"):
            add("free_tasks_only", WARN, f"platform in free-tasks mode: {cfg.get('free_mode_reason', '')[:80]}")
        else:
            add("free_tasks_only", OK, "paid bounties active")

    # 5) claim/submit code path (always dry-run — proves wiring, moves nothing)
    try:
        client = ClawTasksClient(api_key or "", base_url=base_url, live=False)
        c = client.claim("preflight-probe")
        s = client.submit("preflight-probe", "preflight")
        ok = bool(c.get("dry_run")) and bool(s.get("dry_run"))
        add("claim_submit_path", OK if ok else BLOCK, "dry-run claim+submit returned cleanly")
    except Exception as e:
        add("claim_submit_path", BLOCK, f"client path error: {e}")

    # 6) pending() read-only check (only if key present)
    if api_key:
        try:
            client = ClawTasksClient(api_key, base_url=base_url, live=False, get_json=get_json)
            client.pending()
            add("pending_reachable", OK, "GET /agents/me/pending authenticated OK")
        except Exception as e:
            add("pending_reachable", WARN, f"pending() failed (key/account?): {e}")

    go = not any(c["status"] == BLOCK for c in checks)
    return {"go": go, "live": live, "checks": checks}


def _print_report(report: dict[str, Any]) -> None:
    icon = {OK: "✓", WARN: "!", BLOCK: "✗"}
    print("ClawTasks go-live preflight")
    print("-" * 48)
    for c in report["checks"]:
        print(f"  [{icon.get(c['status'], '?')}] {c['name']:<18} {c['detail']}")
    print("-" * 48)
    verdict = "GO" if report["go"] else "NO-GO"
    note = "" if report["go"] else "  (resolve blockers [✗] before going live)"
    print(f"  {verdict}{note}")


def main() -> int:
    report = run_preflight()
    _print_report(report)
    return 0 if report["go"] else 1


if __name__ == "__main__":
    import sys

    sys.exit(main())
