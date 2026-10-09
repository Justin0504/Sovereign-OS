"""
The gate a handoff to another agent passes through.

`DelegationBroker` models authority correctly, and a correct model wired to nothing
protects nothing — the state where the tests are green and production is still open.
This module is the seam between the two: the single call a worker makes before handing
work to something that will touch the world on its behalf.

The decision has to work in a process that may have no broker at all, because the
single-tenant self-host has never had one and breaking it would be a regression. That
makes the default permissive, which is a fail-open default, so the honest answer is to
make the strict posture available and loud rather than to pretend the permissive one is
safe: `SOVEREIGN_STRICT_DELEGATION=1` turns an unauthorized handoff into a refusal, and
a hosted deployment should set it.
"""

from __future__ import annotations

import contextvars
import logging
import os
from contextlib import contextmanager

from sovereign_os.agents.auth import Capability

logger = logging.getLogger(__name__)

STRICT_ENV = "SOVEREIGN_STRICT_DELEGATION"

# What an external coding agent actually does once handed a workspace. It is given a
# directory and a free hand, so the authority required is the authority to change files
# and run commands — not the authority to "use a backend", which is the framing that
# lets this pass unexamined.
EXTERNAL_AGENT_CAPABILITIES = frozenset({
    Capability.WRITE_FILES,
    Capability.EXECUTE_SHELL,
})

_BROKER = None

# The grant the current task is acting under. A tool handler is registered once, globally,
# and invoked later with only its arguments — it has no way to reach the task that called
# it, so the authority has to travel with the execution context rather than the call.
_active_grant: contextvars.ContextVar[str] = contextvars.ContextVar("_active_grant", default="")


@contextmanager
def task_authority(grant_id: str):
    """Bind a task's grant for the duration of its execution."""
    token = _active_grant.set(grant_id or "")
    try:
        yield
    finally:
        _active_grant.reset(token)


def active_grant() -> str:
    return _active_grant.get()


def bind_authority(fn):
    """
    Capture the current authority context and replay it inside a worker thread.

    Captured HERE, in the caller's context, for the same reason the tenant-key helper is:
    `contextvars.copy_context()` called inside the thread copies the thread's own empty
    context and carries nothing.
    """
    ctx = contextvars.copy_context()

    def runner(*args, **kwargs):
        return ctx.run(fn, *args, **kwargs)

    return runner


def set_broker(broker) -> None:
    """Install the process's delegation broker. A tenant-scoped deployment sets the
    tenant's own, so one tenant's authority decisions never consult another's tree."""
    global _BROKER
    _BROKER = broker


def get_broker():
    return _BROKER


def strict_delegation() -> bool:
    return (os.getenv(STRICT_ENV, "") or "").strip().lower() in ("1", "true", "yes")


def authorize_external_agent(
    *, grant_id: str, backend_id: str, task_id: str,
    capabilities=EXTERNAL_AGENT_CAPABILITIES,
) -> tuple[bool, str]:
    """
    Decide whether this task may hand itself to an external agent.

    Returns `(permitted, reason)`. The reason is returned rather than raised because the
    caller has a safe alternative — the native path, which cannot write files or run
    commands — so a refusal should degrade the work rather than fail the mission.
    """
    broker = _BROKER
    if broker is None:
        if strict_delegation():
            return False, (
                f"{STRICT_ENV} is set but no delegation broker is installed, so the "
                f"handoff to {backend_id} cannot be authorized"
            )
        return True, "no delegation governance configured"

    if not grant_id:
        # The task carries no authority, which in a governed process means the handoff
        # was never approved — not that it is harmless.
        if strict_delegation():
            return False, f"task {task_id} carries no delegation grant"
        logger.warning(
            "DELEGATION: task %s is handing work to %s with no grant. Set %s to refuse.",
            task_id, backend_id, STRICT_ENV)
        return True, "ungoverned (no grant on task)"

    missing = []
    for capability in capabilities:
        try:
            broker.authorize(grant_id, capability)
        except Exception as exc:  # noqa: BLE001 - any refusal is a refusal
            missing.append(f"{capability.value} ({exc.__class__.__name__})")

    if missing:
        return False, (
            f"grant {grant_id} does not authorize {', '.join(missing)} — an external "
            f"agent in a workspace needs them, so the handoff would exercise authority "
            f"this task does not hold"
        )
    return True, "authorized"


def authorize_tool_call(
    *, tool_name: str, server_id: str = "", capabilities=None, grant_id: str | None = None
) -> tuple[bool, str]:
    """
    Decide whether the current task may invoke an MCP tool.

    An MCP tool call leaves the process by definition, so CALL_EXTERNAL_API is the floor
    for every one of them. A server whose tools do more than that — a filesystem server
    writes, a shell server executes — declares the extra capabilities rather than having
    them guessed from a tool's name, which would be a classifier standing between an
    agent and the filesystem.
    """
    required = frozenset(capabilities or {Capability.CALL_EXTERNAL_API})
    gid = active_grant() if grant_id is None else grant_id
    label = f"{server_id}:{tool_name}" if server_id else tool_name

    broker = _BROKER
    if broker is None:
        if strict_delegation():
            return False, f"{STRICT_ENV} is set but no delegation broker is installed"
        return True, "no delegation governance configured"

    if not gid:
        if strict_delegation():
            return False, f"no delegation grant is active for tool {label}"
        logger.warning(
            "DELEGATION: tool %s invoked with no active grant. Set %s to refuse.",
            label, STRICT_ENV)
        return True, "ungoverned (no active grant)"

    missing = []
    for capability in required:
        try:
            broker.authorize(gid, capability)
        except Exception as exc:  # noqa: BLE001
            missing.append(f"{capability.value} ({exc.__class__.__name__})")
    if missing:
        return False, f"grant {gid} does not authorize {', '.join(missing)} for tool {label}"
    return True, "authorized"


def delegate_budget(*, grant_id: str, agent_id: str, cents: int, reason: str = "") -> str:
    """
    Carve a sub-grant for an external agent, so what it spends is drawn from the task's
    ceiling rather than from a fresh allowance. Returns the child grant id, or "".
    """
    broker = _BROKER
    if broker is None or not grant_id:
        return ""
    try:
        child = broker.attenuate(
            grant_id, agent_id,
            capabilities=EXTERNAL_AGENT_CAPABILITIES,
            budget_cents=cents, reason=reason,
        )
        return child.grant_id
    except Exception as exc:  # noqa: BLE001
        logger.warning("DELEGATION: could not carve a sub-grant for %s: %s", agent_id, exc)
        return ""


def release_task(task_id: str) -> int:
    """
    Revoke every grant a task issued, once it is done.

    Authority that outlives the work it was issued for is standing privilege wearing a
    task's name, and a sub-agent left holding a live grant is exactly the lease that
    never expired.
    """
    broker = _BROKER
    if broker is None:
        return 0
    try:
        return len(broker.revoke_task(task_id))
    except Exception:  # noqa: BLE001
        logger.warning("DELEGATION: teardown failed for task %s.", task_id, exc_info=True)
        return 0
