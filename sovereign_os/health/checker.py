"""
SovereignHealthCheck: Verifies API connectivity, Ledger integrity, and DB latency at startup.
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class HealthResult:
    """Single check result."""
    name: str
    ok: bool
    latency_ms: float | None = None
    message: str = ""


@dataclass
class SovereignHealthCheck:
    """
    Startup health check: API connectivity, Ledger integrity, Database (Redis) latency.
    """

    ledger: Any = None
    redis_url: str | None = None
    api_base_url: str | None = None

    results: list[HealthResult] = field(default_factory=list)

    def check_ledger_integrity(self) -> HealthResult:
        """Verify Ledger can be read and basic queries succeed."""
        start = time.perf_counter()
        name = "ledger_integrity"
        try:
            if self.ledger is None:
                return HealthResult(name=name, ok=True, message="no ledger configured")
            _ = self.ledger.total_usd_cents()
            _ = self.ledger.total_tokens_by_model()
            latency_ms = (time.perf_counter() - start) * 1000
            return HealthResult(name=name, ok=True, latency_ms=latency_ms, message="ok")
        except Exception as e:
            latency_ms = (time.perf_counter() - start) * 1000
            return HealthResult(name=name, ok=False, latency_ms=latency_ms, message=str(e))

    def check_redis_latency(self) -> HealthResult:
        """Ping Redis and measure latency."""
        start = time.perf_counter()
        name = "redis_latency"
        if not self.redis_url:
            return HealthResult(name=name, ok=True, message="redis not configured")
        try:
            import redis
            r = redis.from_url(self.redis_url)
            r.ping()
            latency_ms = (time.perf_counter() - start) * 1000
            return HealthResult(name=name, ok=True, latency_ms=latency_ms, message="ok")
        except ImportError:
            return HealthResult(name=name, ok=True, message="redis package not installed")
        except Exception as e:
            latency_ms = (time.perf_counter() - start) * 1000
            return HealthResult(name=name, ok=False, latency_ms=latency_ms, message=str(e))

    def check_api_connectivity(self) -> HealthResult:
        """GET health endpoint or base URL to verify API is reachable."""
        start = time.perf_counter()
        name = "api_connectivity"
        if not self.api_base_url:
            return HealthResult(name=name, ok=True, message="api not configured")
        try:
            import urllib.request
            url = self.api_base_url.rstrip("/") + "/health"
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status in (200, 204):
                    latency_ms = (time.perf_counter() - start) * 1000
                    return HealthResult(name=name, ok=True, latency_ms=latency_ms, message="ok")
        except Exception as e:
            latency_ms = (time.perf_counter() - start) * 1000
            return HealthResult(name=name, ok=False, latency_ms=latency_ms, message=str(e))
        return HealthResult(name=name, ok=False, message="unexpected response")

    def run(self) -> list[HealthResult]:
        """Run all checks and return results."""
        self.results = [
            self.check_ledger_integrity(),
            self.check_redis_latency(),
            self.check_api_connectivity(),
        ]
        return self.results

    def is_healthy(self) -> bool:
        """Return True if all configured checks passed."""
        self.run()
        return all(r.ok for r in self.results)


def run_health_check(
    ledger: Any = None,
    redis_url: str | None = None,
    api_base_url: str | None = None,
) -> bool:
    """Convenience: run SovereignHealthCheck and log results. Returns is_healthy."""
    check = SovereignHealthCheck(ledger=ledger, redis_url=redis_url, api_base_url=api_base_url)
    results = check.run()
    for r in results:
        if r.ok:
            logger.info("HEALTH [%s] ok%s", r.name, f" {r.latency_ms:.1f}ms" if r.latency_ms is not None else "")
        else:
            logger.warning("HEALTH [%s] fail: %s", r.name, r.message)
    return check.is_healthy()
