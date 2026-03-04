"""
Global rate limiter for API calls to prevent 429 (Too Many Requests) during high-concurrency bursts.
"""

import asyncio
import time
from collections import deque
from threading import Lock


class AsyncRateLimiter:
    """
    Token-bucket style rate limiter for async code.
    Call acquire() before each API call; it will wait if the rate would be exceeded.
    """

    def __init__(self, *, max_calls: int = 60, period_seconds: float = 60.0) -> None:
        self._max_calls = max_calls
        self._period = period_seconds
        self._timestamps: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            # Drop timestamps outside the current window
            while self._timestamps and self._timestamps[0] < now - self._period:
                self._timestamps.popleft()
            if len(self._timestamps) >= self._max_calls:
                wait_until = self._timestamps[0] + self._period - now
                if wait_until > 0:
                    await asyncio.sleep(wait_until)
                now = time.monotonic()
                self._timestamps.popleft()
            self._timestamps.append(now)


# Module-level default limiter; can be overridden by engine or workers.
_global_limiter: AsyncRateLimiter | None = None
_global_limiter_lock = Lock()


def get_global_rate_limiter() -> AsyncRateLimiter | None:
    return _global_limiter


def set_global_rate_limiter(limiter: AsyncRateLimiter | None) -> None:
    global _global_limiter
    with _global_limiter_lock:
        _global_limiter = limiter


def create_default_rate_limiter(*, max_calls: int = 60, period_seconds: float = 60.0) -> AsyncRateLimiter:
    """Create and set the global limiter (e.g. 60 calls per minute)."""
    limiter = AsyncRateLimiter(max_calls=max_calls, period_seconds=period_seconds)
    set_global_rate_limiter(limiter)
    return limiter
