"""
Governed delegation: authority that survives being handed to another agent.

A single-layer agent is easy to govern — one identity, one budget, one audit line. All
of that becomes hard the moment an agent hands work to another agent, which this system
already does: `CodeAssistantWorker._maybe_delegate` passes an entire task, plus a
workspace path, to an external coding agent. The budget gate approved a `code_assistant`
task; what then ran was a separate agent with its own powers, and nothing in the
authority layer knew the handoff had happened.

That is the structural failure a governance layer has when it wraps the *planner* rather
than the *effects*: every check passes, and the thing that actually touches the world was
never checked at all.

The model here is object-capability discipline rather than role lookup. A delegation
grant is a token of authority that can be handed on, and handing it on can only ever
*narrow* it:

**Authority flows down and only shrinks.** A child's capability set is a subset of its
parent's, always. This is what prevents the confused deputy — the failure where a
low-authority agent gets a high-authority one to act on its behalf. Checking the callee's
own eligibility instead would permit exactly that, because the callee really is eligible;
it is the *chain* that is not.

**Effective authority is the intersection of the grant and the holder's own eligibility.**
Both must permit an action. A grant cannot give an untrusted agent powers it has not
earned, and an agent's own standing cannot exceed what it was delegated. Either alone is
a hole: the first lets a compromised parent hand out authority it should not, the second
is the confused deputy again.

**Budget is conserved, not re-issued.** A child draws from the parent's remaining
allowance, so the total a subtree can spend is bounded by its root no matter how wide or
deep it grows. Giving each child a fresh ceiling is the delegation equivalent of the
task-splitting attack: every individual approval is legitimate and the total is not.

**Revocation cascades.** Revoking a grant revokes its descendants in the same act,
because authority a child holds exists only as a slice of its parent's.

**Every grant carries its provenance.** The principal chain travels with the authority,
so a leaf action is attributable to the root mission without reconstructing anything from
logs that may not have been written.
"""

from __future__ import annotations

import itertools
import logging
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field

from sovereign_os.agents.auth import Capability

logger = logging.getLogger(__name__)

DEFAULT_MAX_DEPTH = 4
"""How many times authority may be handed on. Unbounded delegation is unbounded
recursion with a budget attached; agents that can spawn agents will."""

DEFAULT_MAX_FANOUT = 8
"""Children per grant. Bounds breadth the way depth bounds length."""


class DelegationError(RuntimeError):
    """Base for refusals in the delegation layer."""


class AttenuationError(DelegationError):
    """A delegation tried to widen authority instead of narrowing it."""


class BudgetExceeded(DelegationError):
    """A delegation or spend would exceed what an ancestor still holds."""


class DepthExceeded(DelegationError):
    """The delegation chain hit its depth or fan-out limit."""


class GrantNotLive(DelegationError):
    """The grant is revoked, expired, or unknown."""


@dataclass
class DelegationGrant:
    """
    One slice of authority, held by one agent, for one task.

    A budget is a *commitment*, not a running total. Every cent of `budget_cents` is
    either spent by this holder directly or reserved to a child, and those are disjoint:

        direct_spent_cents + delegated_cents <= budget_cents

    Tracking only "what has been spent so far" is the hole this replaces. Delegating does
    not spend, so a parent checking spend alone will hand out 600c twice from a 1000c
    budget and both checks pass — the task-splitting attack, one layer up. Reserving at
    delegation time makes the ceiling hold structurally rather than by vigilance.
    """

    grant_id: str
    agent_id: str
    task_id: str
    capabilities: frozenset[Capability]
    budget_cents: int
    parent_id: str | None = None
    depth: int = 0
    direct_spent_cents: int = 0
    """Charged at this grant by its own holder, excluding descendants."""

    delegated_cents: int = 0
    """Reserved to children. Released again if a child is revoked unspent."""

    granted_at: float = 0.0
    expires_at: float | None = None
    revoked: bool = False
    principal_chain: tuple[str, ...] = ()
    """Agent ids from the root to this holder — the provenance of the authority."""

    reason: str = ""

    @property
    def available_cents(self) -> int:
        """Uncommitted: free to spend here or to reserve to a new child."""
        return max(0, self.budget_cents - self.direct_spent_cents - self.delegated_cents)

    def is_live(self, now: float) -> bool:
        if self.revoked:
            return False
        if self.expires_at is not None and now >= self.expires_at:
            return False
        return True

    def permits(self, capability: Capability) -> bool:
        return capability in self.capabilities

    def as_dict(self) -> dict:
        return {
            "grant_id": self.grant_id, "agent_id": self.agent_id, "task_id": self.task_id,
            "capabilities": sorted(c.value for c in self.capabilities),
            "budget_cents": self.budget_cents,
            "direct_spent_cents": self.direct_spent_cents,
            "delegated_cents": self.delegated_cents,
            "available_cents": self.available_cents, "parent_id": self.parent_id,
            "depth": self.depth, "revoked": self.revoked,
            "principal_chain": list(self.principal_chain), "reason": self.reason,
        }


class DelegationBroker:
    """
    Issues, narrows, and revokes delegation grants, and is the single place a delegated
    action is authorized.

    `eligibility` is the second half of the intersection rule: a callable deciding
    whether an agent has itself earned a capability, normally
    `SovereignAuth.check_permission_for`. Passing None disables that half, which is
    useful in tests and wrong in production — hence `strict_eligibility`.
    """

    def __init__(
        self,
        *,
        eligibility=None,
        max_depth: int = DEFAULT_MAX_DEPTH,
        max_fanout: int = DEFAULT_MAX_FANOUT,
        clock=time.monotonic,
        strict_eligibility: bool = False,
    ) -> None:
        if strict_eligibility and eligibility is None:
            raise ValueError(
                "strict_eligibility requires an eligibility check; without one a grant "
                "alone would confer authority the holder never earned."
            )
        self._eligibility = eligibility
        self._max_depth = max(0, int(max_depth))
        self._max_fanout = max(1, int(max_fanout))
        self._clock = clock
        self._grants: dict[str, DelegationGrant] = {}
        self._children: dict[str, list[str]] = {}
        self._seq = itertools.count(1)

    # ------------------------------------------------------------------ issuing
    def root(
        self,
        agent_id: str,
        *,
        task_id: str,
        capabilities: Iterable[Capability],
        budget_cents: int,
        ttl_seconds: float | None = None,
        reason: str = "",
    ) -> DelegationGrant:
        """
        Open a delegation tree. The root is bounded by the holder's own eligibility, so
        a mission cannot start out holding more than the agent has earned.
        """
        caps = frozenset(capabilities)
        allowed = self._eligible_subset(agent_id, caps)
        if allowed != caps:
            denied = sorted(c.value for c in (caps - allowed))
            logger.info("DELEGATION: root grant narrowed for [%s]; not eligible for %s.",
                        agent_id, denied)
        now = self._clock()
        grant = DelegationGrant(
            grant_id=f"grant-{next(self._seq)}",
            agent_id=agent_id, task_id=task_id,
            capabilities=allowed,
            budget_cents=max(0, int(budget_cents)),
            parent_id=None, depth=0, granted_at=now,
            expires_at=(now + ttl_seconds) if ttl_seconds is not None else None,
            principal_chain=(agent_id,), reason=reason,
        )
        self._grants[grant.grant_id] = grant
        self._children[grant.grant_id] = []
        return grant

    def attenuate(
        self,
        parent_id: str,
        agent_id: str,
        *,
        capabilities: Iterable[Capability],
        budget_cents: int,
        task_id: str | None = None,
        ttl_seconds: float | None = None,
        reason: str = "",
    ) -> DelegationGrant:
        """
        Hand a narrowed slice of a grant to another agent.

        Refuses rather than silently trimming when asked to widen: a caller that
        believes it delegated `EXECUTE_SHELL` and received a grant without it would
        carry on and fail somewhere less legible. Narrowing by the callee's own
        eligibility is different — that is the intersection rule doing its job, and it
        is logged.
        """
        parent = self._live_or_raise(parent_id)
        requested = frozenset(capabilities)

        if not requested <= parent.capabilities:
            excess = sorted(c.value for c in (requested - parent.capabilities))
            raise AttenuationError(
                f"delegation would widen authority: grant {parent_id} does not hold "
                f"{excess}. Authority can only be narrowed as it is handed on."
            )

        if parent.depth + 1 > self._max_depth:
            raise DepthExceeded(
                f"delegation depth {parent.depth + 1} exceeds the limit of "
                f"{self._max_depth}."
            )
        if len(self._children.get(parent_id, ())) >= self._max_fanout:
            raise DepthExceeded(
                f"grant {parent_id} already has {self._max_fanout} children, the fan-out "
                f"limit."
            )

        amount = max(0, int(budget_cents))
        if amount > parent.available_cents:
            raise BudgetExceeded(
                f"delegating {amount}c exceeds the {parent.available_cents}c still "
                f"uncommitted in grant {parent_id}. A child's budget is reserved from "
                f"its parent, not issued fresh — otherwise every delegation passes its "
                f"own check and the subtree total is unbounded."
            )

        granted = self._eligible_subset(agent_id, requested)
        if granted != requested:
            logger.info(
                "DELEGATION: narrowed for [%s]; holds grant for %s but has not earned %s.",
                agent_id, sorted(c.value for c in requested),
                sorted(c.value for c in (requested - granted)),
            )

        now = self._clock()
        # A child may not outlive its parent: an expiry past the parent's would leave
        # authority alive after the thing it was carved from is gone.
        expires = (now + ttl_seconds) if ttl_seconds is not None else parent.expires_at
        if parent.expires_at is not None:
            expires = parent.expires_at if expires is None else min(expires, parent.expires_at)

        grant = DelegationGrant(
            grant_id=f"grant-{next(self._seq)}",
            agent_id=agent_id,
            task_id=task_id or parent.task_id,
            capabilities=granted,
            budget_cents=amount,
            parent_id=parent.grant_id,
            depth=parent.depth + 1,
            granted_at=now, expires_at=expires,
            principal_chain=parent.principal_chain + (agent_id,),
            reason=reason,
        )
        self._grants[grant.grant_id] = grant
        self._children[grant.grant_id] = []
        self._children.setdefault(parent.grant_id, []).append(grant.grant_id)
        parent.delegated_cents += amount        # reserved, not merely promised
        logger.info("DELEGATION: [%s] -> [%s] depth=%d caps=%s budget=%dc (chain: %s)",
                    parent.agent_id, agent_id, grant.depth,
                    sorted(c.value for c in granted), amount,
                    " > ".join(grant.principal_chain))
        return grant

    # ------------------------------------------------------------- authorizing
    def authorize(self, grant_id: str, capability: Capability) -> DelegationGrant:
        """
        The single gate a delegated action passes through. Returns the grant or raises.

        Re-checks eligibility at use time rather than trusting the grant alone, so trust
        lost after issuance takes effect immediately instead of at the next handoff.
        """
        grant = self._live_or_raise(grant_id)
        if not grant.permits(capability):
            raise GrantNotLive(
                f"grant {grant_id} does not carry {capability.value}; it holds "
                f"{sorted(c.value for c in grant.capabilities)}."
            )
        if not self._is_eligible(grant.agent_id, capability):
            raise GrantNotLive(
                f"[{grant.agent_id}] is no longer eligible for {capability.value}; a "
                f"grant does not outrank the holder's own standing."
            )
        return grant

    def spend(self, grant_id: str, cents: int) -> None:
        """
        Charge a grant for work its own holder did.

        Only this grant's headroom is checked, and that is sufficient rather than lax:
        its budget was already reserved out of its parent's at delegation time, so a
        child spending inside its own ceiling cannot breach an ancestor. Walking the
        ancestry here as well would double-count the same cents.

        The whole ancestry is still required to be live, so a spend under a revoked or
        expired parent is refused.
        """
        amount = max(0, int(cents))
        if amount == 0:
            return
        chain = list(self._ancestry(grant_id))     # raises if any ancestor is not live
        grant = chain[0]
        if amount > grant.available_cents:
            raise BudgetExceeded(
                f"spending {amount}c would exceed grant {grant.grant_id} "
                f"({grant.available_cents}c uncommitted, held by [{grant.agent_id}] at "
                f"depth {grant.depth})."
            )
        grant.direct_spent_cents += amount

    def consumed_cents(self, grant_id: str) -> int:
        """
        What a subtree has actually spent: this grant plus every descendant.

        Derived rather than stored, so it cannot drift from the per-grant records the
        way a denormalised running total would.
        """
        grant = self._grants.get(grant_id)
        if grant is None:
            return 0
        return grant.direct_spent_cents + sum(
            self.consumed_cents(c) for c in self._children.get(grant_id, ()))

    # ------------------------------------------------------------- revocation
    def revoke(self, grant_id: str) -> list[str]:
        """
        Revoke a grant and everything carved from it. Returns the ids revoked.

        Cascading is not a convenience: a child's authority exists only as a slice of
        its parent's, so leaving descendants live would leave authority in the world
        with nothing backing it.
        """
        target = self._grants.get(grant_id)
        # Release the unconsumed part of this grant's reservation back to its parent, so
        # a cancelled branch does not leave budget stranded for the rest of the mission.
        if target is not None and not target.revoked and target.parent_id:
            parent = self._grants.get(target.parent_id)
            if parent is not None:
                unspent = max(0, target.budget_cents - self.consumed_cents(grant_id))
                parent.delegated_cents = max(0, parent.delegated_cents - unspent)

        revoked: list[str] = []
        for grant in self._subtree(grant_id):
            if not grant.revoked:
                grant.revoked = True
                revoked.append(grant.grant_id)
        if revoked:
            logger.info("DELEGATION: revoked %d grant(s) under %s.", len(revoked), grant_id)
        return revoked

    def revoke_task(self, task_id: str) -> list[str]:
        """Revoke every grant issued for a task — the end-of-task teardown."""
        roots = [g.grant_id for g in self._grants.values()
                 if g.task_id == task_id and not g.revoked]
        return [gid for root in roots for gid in self.revoke(root)]

    # ------------------------------------------------------------------ audit
    def get(self, grant_id: str) -> DelegationGrant | None:
        return self._grants.get(grant_id)

    def chain(self, grant_id: str) -> tuple[str, ...]:
        """The principal path from root to holder, for attributing a leaf action."""
        grant = self._grants.get(grant_id)
        return grant.principal_chain if grant else ()

    def tree(self, grant_id: str) -> dict:
        """The delegation subtree, for an audit record or an operator view."""
        grant = self._grants.get(grant_id)
        if grant is None:
            return {}
        return {
            **grant.as_dict(),
            "children": [self.tree(c) for c in self._children.get(grant_id, ())],
        }

    def active_grants(self) -> list[DelegationGrant]:
        now = self._clock()
        return [g for g in self._grants.values() if g.is_live(now)]

    # -------------------------------------------------------------- internals
    def _live_or_raise(self, grant_id: str) -> DelegationGrant:
        grant = self._grants.get(grant_id)
        if grant is None:
            raise GrantNotLive(f"unknown grant {grant_id}.")
        if not grant.is_live(self._clock()):
            raise GrantNotLive(
                f"grant {grant_id} is {'revoked' if grant.revoked else 'expired'}."
            )
        return grant

    def _ancestry(self, grant_id: str) -> Iterator[DelegationGrant]:
        """The grant and each ancestor, nearest first. Every one must be live."""
        seen: set[str] = set()
        current: str | None = grant_id
        while current is not None:
            if current in seen:        # a malformed tree must not loop forever
                return
            seen.add(current)
            grant = self._live_or_raise(current)
            yield grant
            current = grant.parent_id

    def _subtree(self, grant_id: str) -> Iterator[DelegationGrant]:
        stack = [grant_id]
        seen: set[str] = set()
        while stack:
            gid = stack.pop()
            if gid in seen:
                continue
            seen.add(gid)
            grant = self._grants.get(gid)
            if grant is None:
                continue
            yield grant
            stack.extend(self._children.get(gid, ()))

    def _is_eligible(self, agent_id: str, capability: Capability) -> bool:
        if self._eligibility is None:
            return True
        try:
            return bool(self._eligibility(agent_id, capability))
        except Exception:  # noqa: BLE001
            # An eligibility check that errors must not read as permission.
            logger.warning("DELEGATION: eligibility check failed for [%s] %s; denying.",
                           agent_id, capability.value, exc_info=True)
            return False

    def _eligible_subset(
        self, agent_id: str, capabilities: frozenset[Capability]
    ) -> frozenset[Capability]:
        return frozenset(c for c in capabilities if self._is_eligible(agent_id, c))
