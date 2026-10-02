"""
Rate limiting for a publicly reachable control plane.

An open signup endpoint that also executes agent missions is an unusually expensive
thing to leave unmetered: one request does not cost a database row, it costs model
tokens and a worker slot. The plan's daily caps already bound what a *tenant* consumes,
but they are enforced after authentication, so they do nothing about the two cases that
matter before it — someone minting tenants in a loop, and someone hammering an endpoint
to find a credential.

Two different limiters, because the quantities are different:

**Token bucket** for sustained work. A bucket refills continuously, so a caller gets a
burst allowance and then settles to a steady rate — which is how real usage looks, and
why a fixed window is the wrong shape: it lets a caller spend the whole window's budget
in its last second and the next window's in its first.

**Failure counter** for authentication. Failed attempts are what matter there, not
request volume, so a legitimate caller is never slowed by its own traffic while a
guesser is stopped after a handful of misses.

In-process and therefore per-worker: two uvicorn workers enforce roughly twice the limit.
That is a deliberate trade for a beta — it holds the line against the loop and the
guesser without a Redis dependency — and it is the first thing to replace with shared
state when there is more than one box. `limits_are_process_local()` exists so a
deployment can say so out loud rather than discovering it.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


@dataclass
class _Bucket:
    tokens: float
    updated: float


class RateLimiter:
    """
    Token-bucket limiter keyed by caller.

    `capacity` is the burst a caller may spend at once; `refill_per_second` the rate it
    returns at. Thread-safe, since uvicorn serves requests from a pool.
    """

    def __init__(self, capacity: float, refill_per_second: float, *, clock=time.monotonic) -> None:
        if capacity <= 0 or refill_per_second <= 0:
            raise ValueError("capacity and refill_per_second must both be positive")
        self.capacity = float(capacity)
        self.refill_per_second = float(refill_per_second)
        self._clock = clock
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()

    def check(self, key: str, cost: float = 1.0) -> tuple[bool, float]:
        """
        Try to spend `cost` for `key`. Returns (allowed, retry_after_seconds).

        Nothing is deducted when the call is refused, so a rejected caller does not dig
        itself deeper and take longer to recover than an idle one.
        """
        now = self._clock()
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _Bucket(tokens=self.capacity, updated=now)
                self._buckets[key] = bucket
            elapsed = max(0.0, now - bucket.updated)
            bucket.tokens = min(self.capacity, bucket.tokens + elapsed * self.refill_per_second)
            bucket.updated = now

            if bucket.tokens >= cost:
                bucket.tokens -= cost
                return True, 0.0
            shortfall = cost - bucket.tokens
            return False, shortfall / self.refill_per_second

    def reset(self, key: str | None = None) -> None:
        with self._lock:
            if key is None:
                self._buckets.clear()
            else:
                self._buckets.pop(key, None)

    def prune(self, max_idle_seconds: float = 3600.0) -> int:
        """
        Drop buckets idle long enough to have refilled anyway.

        Without this the key space is an unbounded dict keyed by client IP — a slow
        memory leak that an attacker can drive deliberately.
        """
        now = self._clock()
        with self._lock:
            stale = []
            for key, bucket in self._buckets.items():
                elapsed = max(0.0, now - bucket.updated)
                if elapsed <= max_idle_seconds:
                    continue
                # Tokens refill lazily, only on check(), so the stored count is stale by
                # exactly the interval that makes a bucket eligible here. Reading it
                # directly means a bucket that has been used even once never prunes —
                # which is to say the bound it exists to provide never applies.
                projected = min(self.capacity, bucket.tokens + elapsed * self.refill_per_second)
                if projected >= self.capacity - 1e-9:
                    stale.append(key)
            for key in stale:
                del self._buckets[key]
        return len(stale)

    @property
    def tracked_keys(self) -> int:
        with self._lock:
            return len(self._buckets)


@dataclass
class AuthThrottle:
    """
    Lockout after repeated authentication failures.

    Counts failures rather than requests: an API key is 32 bytes of entropy, so this is
    not really about making brute force infeasible — it already is — but about denying a
    cheap oracle and keeping a credential-stuffing run from costing the host anything.
    """

    max_failures: int = 10
    window_seconds: float = 300.0
    clock: object = time.monotonic
    _failures: dict[str, list[float]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def _now(self) -> float:
        return self.clock()  # type: ignore[operator]

    def record_failure(self, key: str) -> None:
        now = self._now()
        with self._lock:
            hits = [t for t in self._failures.get(key, ()) if now - t < self.window_seconds]
            hits.append(now)
            self._failures[key] = hits

    def record_success(self, key: str) -> None:
        """A success clears the record, so a user who mistypes then succeeds is not
        half-locked for the rest of the window."""
        with self._lock:
            self._failures.pop(key, None)

    def is_locked(self, key: str) -> tuple[bool, float]:
        now = self._now()
        with self._lock:
            hits = [t for t in self._failures.get(key, ()) if now - t < self.window_seconds]
            self._failures[key] = hits
            if len(hits) >= self.max_failures:
                return True, max(0.0, self.window_seconds - (now - hits[0]))
        return False, 0.0

    def reset(self, key: str | None = None) -> None:
        with self._lock:
            if key is None:
                self._failures.clear()
            else:
                self._failures.pop(key, None)


def limits_are_process_local() -> bool:
    """
    True — stated as a function so a deployment can surface it rather than assume
    otherwise. With N workers the effective limit is N times the configured one.
    """
    return True


def client_key(request) -> str:
    """
    Identify a caller for limiting.

    Honours `X-Forwarded-For` only when the deployment says it is behind a proxy, since
    the header is caller-controlled: trusting it unconditionally lets anyone forge a
    fresh identity per request and makes the limiter decorative.
    """
    import os

    if (os.getenv("SOVEREIGN_TRUST_PROXY", "") or "").strip().lower() in ("1", "true", "yes"):
        forwarded = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
        if forwarded:
            return forwarded
    client = getattr(request, "client", None)
    return getattr(client, "host", None) or "unknown"
