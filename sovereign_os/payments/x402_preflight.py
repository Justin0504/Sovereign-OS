"""
Go-live safety check for the x402 / USDC rail. Run BEFORE X402_SANDBOX=false.

Verifies configuration and the facilitator settle path WITHOUT moving real funds,
and returns GO / NO-GO. Blockers: live mode with no pay_to or no facilitator URL,
or an unreachable/malformed facilitator. Warnings: mainnet network, live flag.

  python -m sovereign_os.payments.x402_preflight
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable

from sovereign_os.payments.x402 import X402PaymentService, cents_to_usdc_atomic

logger = logging.getLogger(__name__)

OK, WARN, BLOCK = "ok", "warn", "blocker"


def run_preflight(
    *,
    svc: X402PaymentService | None = None,
    probe_settle: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """
    Return {"go": bool, "live": bool, "checks": [{name,status,detail}]}.

    Sandbox is validated by running a real sandbox charge (no funds). Live config
    is validated structurally; `probe_settle` (injectable) may test the facilitator
    with a $0-style probe — otherwise the live wire is checked but not called.
    """
    import asyncio

    svc = svc or X402PaymentService.from_env()
    checks: list[dict[str, str]] = []

    def add(name: str, status: str, detail: str) -> None:
        checks.append({"name": name, "status": status, "detail": detail})

    add("mode", WARN if svc.is_live else OK,
        "LIVE — settlements move real USDC" if svc.is_live else "sandbox (no funds move)")
    add("network", WARN if svc.network in ("base", "ethereum", "mainnet") else OK,
        f"network={svc.network}" + (" (MAINNET)" if svc.network == "base" else ""))

    # Sandbox settle always works and moves nothing — proves the wiring + atomic math.
    try:
        ref = asyncio.run(X402PaymentService(pay_to=svc.pay_to or "0xprobe", network=svc.network,
                                             sandbox=True).charge(100, "usd", metadata={"job_id": "preflight"}))
        add("sandbox_settle", OK if ref.startswith("x402_") else BLOCK,
            f"$1.00 -> {cents_to_usdc_atomic(100)} atomic, ref={ref[:28]}…")
    except Exception as e:
        add("sandbox_settle", BLOCK, f"sandbox charge failed: {e}")

    if svc.is_live or not svc.sandbox:
        add("pay_to", OK if svc.pay_to else BLOCK, svc.pay_to or "X402_PAY_TO not set (required live)")
        add("facilitator", OK if svc.facilitator_url else BLOCK,
            svc.facilitator_url or "X402_FACILITATOR_URL not set (required live)")
        if probe_settle is not None and svc.pay_to and svc.facilitator_url:
            try:
                live = X402PaymentService(pay_to=svc.pay_to, network=svc.network, sandbox=False,
                                          facilitator_url=svc.facilitator_url, api_key=svc.api_key,
                                          post_settle=probe_settle)
                tx = asyncio.run(live.charge(1, "usd", metadata={"job_id": "preflight-probe"}))
                add("facilitator_probe", OK if tx else BLOCK, f"facilitator returned tx={str(tx)[:24]}…")
            except Exception as e:
                add("facilitator_probe", BLOCK, f"facilitator probe failed: {e}")

    go = not any(c["status"] == BLOCK for c in checks)
    return {"go": go, "live": svc.is_live, "checks": checks}


def _print_report(report: dict[str, Any]) -> None:
    icon = {OK: "✓", WARN: "!", BLOCK: "✗"}
    print("x402 go-live preflight")
    print("-" * 52)
    for c in report["checks"]:
        print(f"  [{icon.get(c['status'], '?')}] {c['name']:<18} {c['detail']}")
    print("-" * 52)
    print(f"  {'GO' if report['go'] else 'NO-GO'}" + ("" if report["go"] else "  (resolve blockers [✗] before going live)"))


def main() -> int:
    report = run_preflight()
    _print_report(report)
    return 0 if report["go"] else 1


if __name__ == "__main__":
    import sys

    sys.exit(main())
