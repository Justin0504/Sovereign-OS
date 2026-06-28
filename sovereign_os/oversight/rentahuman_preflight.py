"""
Go-live safety check for RentAHuman outbound oversight. Run BEFORE setting
RENTAHUMAN_LIVE=true (funding escrow and releasing payment move real money).

Verifies configuration and the post/fund/release code path WITHOUT moving funds,
and returns GO / NO-GO. Blockers: missing key when live, unreachable account.
Warnings: live flag set, no key in dry-run.

  python -m sovereign_os.oversight.rentahuman_preflight
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable

from sovereign_os.oversight.rentahuman import DEFAULT_BASE_URL, RentAHumanClient, _http_get_json

logger = logging.getLogger(__name__)

OK, WARN, BLOCK = "ok", "warn", "blocker"


def run_preflight(
    *,
    api_key: str | None = None,
    live: bool | None = None,
    base_url: str | None = None,
    get_json: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Return {"go": bool, "live": bool, "checks": [{name,status,detail}]}. Moves no funds."""
    api_key = api_key if api_key is not None else os.getenv("RENTAHUMAN_API_KEY", "")
    live = live if live is not None else os.getenv("RENTAHUMAN_LIVE", "").lower() in ("1", "true", "yes")
    base_url = (base_url or os.getenv("RENTAHUMAN_BASE_URL", DEFAULT_BASE_URL)).rstrip("/")
    get_json = get_json or _http_get_json

    checks: list[dict[str, str]] = []

    def add(name: str, status: str, detail: str) -> None:
        checks.append({"name": name, "status": status, "detail": detail})

    add("mode", WARN if live else OK, "LIVE — funding/releasing moves real money" if live else "dry-run (no funds move)")

    if api_key:
        add("api_key", OK, "present")
    else:
        add("api_key", BLOCK if live else WARN, "RENTAHUMAN_API_KEY not set (required for live post/fund/release)")

    # post -> fund -> release code path, always dry-run (proves wiring, moves nothing).
    try:
        client = RentAHumanClient(api_key or "", base_url=base_url, live=False)
        b = client.post_bounty(title="preflight", description="preflight probe", price_cents=100)
        f = client.fund_escrow(b["id"], 100)
        r = client.release(f["id"])
        ok = all(x.get("dry_run") for x in (b, f, r))
        add("post_fund_release_path", OK if ok else BLOCK, "dry-run post+fund+release returned cleanly")
    except Exception as e:
        add("post_fund_release_path", BLOCK, f"client path error: {e}")

    # Account reachability (read-only) when a key is present.
    if api_key:
        try:
            client = RentAHumanClient(api_key, base_url=base_url, live=True, get_json=get_json)
            client.list_rentals()
            add("account_reachable", OK, "GET /escrow/agent-rentals authenticated OK")
        except Exception as e:
            add("account_reachable", BLOCK if live else WARN, f"agent-rentals failed (key/account?): {e}")

    go = not any(c["status"] == BLOCK for c in checks)
    return {"go": go, "live": live, "checks": checks}


def _print_report(report: dict[str, Any]) -> None:
    icon = {OK: "✓", WARN: "!", BLOCK: "✗"}
    print("RentAHuman go-live preflight")
    print("-" * 50)
    for c in report["checks"]:
        print(f"  [{icon.get(c['status'], '?')}] {c['name']:<22} {c['detail']}")
    print("-" * 50)
    print(f"  {'GO' if report['go'] else 'NO-GO'}" + ("" if report["go"] else "  (resolve blockers [✗] before going live)"))


def main() -> int:
    report = run_preflight()
    _print_report(report)
    return 0 if report["go"] else 1


if __name__ == "__main__":
    import sys

    sys.exit(main())
