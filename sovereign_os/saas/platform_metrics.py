"""
Operational metrics for the hosted control plane.

The self-host dashboard exposes /metrics; the hosted service had no observability
endpoint at all, so an operator running it for other people had no way to see signup
rate, mission throughput, or the auth-failure rate that would signal an attack. This adds
one, with two constraints that come straight from the fact that it is multi-tenant.

1. **Aggregate only, never per-tenant.** A metric labelled by tenant id would let anyone
   who can scrape it enumerate the tenants and watch each one's activity -- the same
   cross-tenant leak the rest of the isolation work exists to prevent, handed out through
   the monitoring port. Every counter here is a platform total. Nothing is labelled by
   tenant.

2. **Off unless an operator opts in.** Even aggregates (how many tenants, how many
   missions) are business-sensitive, and an open Prometheus endpoint is an
   information-disclosure surface. The endpoint is disabled unless SOVEREIGN_METRICS_TOKEN
   is set, and then requires it as a bearer token. Fail closed: no token, no metrics.

Counters are in-process and reset on restart, consistent with the rate limiter and tenant
store being in-process -- which replicas:1 already pins as a correctness constraint.
"""

from __future__ import annotations

import os
import threading

_lock = threading.Lock()

# name -> (help text, value). Counters only; gauges are computed at render time from the
# store so they cannot drift from reality.
_counters: dict[str, list] = {
    "sovereign_signups_total": ["Workspaces created", 0],
    "sovereign_missions_total": ["Missions run (any outcome)", 0],
    "sovereign_missions_passed_total": ["Missions whose every task passed audit", 0],
    "sovereign_missions_failed_total": ["Missions that raised or failed audit", 0],
    "sovereign_demo_runs_total": ["Keyless demo runs", 0],
    "sovereign_auth_failures_total": ["Rejected tenant-key presentations", 0],
    "sovereign_auth_lockouts_total": ["Callers locked out after repeated auth failure", 0],
    "sovereign_config_rejected_total": ["Config writes refused at validation", 0],
}


def incr(name: str, by: int = 1) -> None:
    with _lock:
        slot = _counters.get(name)
        if slot is not None:
            slot[1] += by


def reset() -> None:
    """For tests. Zero every counter."""
    with _lock:
        for slot in _counters.values():
            slot[1] = 0


def metrics_token() -> str:
    return (os.getenv("SOVEREIGN_METRICS_TOKEN") or "").strip()


def enabled() -> bool:
    return bool(metrics_token())


def render(gauges: dict[str, tuple[str, float]] | None = None) -> str:
    """
    Prometheus text exposition. `gauges` maps metric name -> (help, value) for values
    read live from the store at scrape time (e.g. the current tenant count), so they are
    never stored and cannot go stale.
    """
    lines: list[str] = []
    with _lock:
        items = [(n, h, v) for n, (h, v) in _counters.items()]
    for name, help_text, value in items:
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} counter")
        lines.append(f"{name} {value}")
    for name, (help_text, value) in (gauges or {}).items():
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} gauge")
        lines.append(f"{name} {value}")
    return "\n".join(lines) + "\n"
