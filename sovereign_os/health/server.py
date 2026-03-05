"""
Minimal HTTP server for /health endpoint (SovereignHealthCheck).
Used by Docker healthcheck or external probes.
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)

try:
    from fastapi import FastAPI
    from uvicorn import run as uvicorn_run
    _FASTAPI_AVAILABLE = True
except ImportError:
    _FASTAPI_AVAILABLE = False


def create_health_app(
    ledger: Any = None,
    redis_url: str | None = None,
) -> "FastAPI":
    """Create FastAPI app with GET /health that runs SovereignHealthCheck."""
    if not _FASTAPI_AVAILABLE:
        raise ImportError("fastapi and uvicorn required for health server; pip install fastapi uvicorn")
    from sovereign_os.health.checker import SovereignHealthCheck

    app = FastAPI(title="Sovereign-OS Health", version="0.1.0")

    @app.get("/health")
    def health():
        check = SovereignHealthCheck(ledger=ledger, redis_url=redis_url, api_base_url=None)
        results = check.run()
        ok = all(r.ok for r in results)
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=200 if ok else 503,
            content={
                "status": "ok" if ok else "degraded",
                "checks": [
                    {"name": r.name, "ok": r.ok, "latency_ms": r.latency_ms, "message": r.message}
                    for r in results
                ],
            },
        )

    return app


def run_health_server(
    host: str = "0.0.0.0",
    port: int = 8080,
    ledger: Any = None,
    redis_url: str | None = None,
) -> None:
    """Run uvicorn with the health app (blocking)."""
    if not _FASTAPI_AVAILABLE:
        logger.warning("FastAPI/uvicorn not installed; health server skipped.")
        return
    app = create_health_app(ledger=ledger, redis_url=redis_url)
    uvicorn_run(app, host=host, port=port, log_level="warning")
