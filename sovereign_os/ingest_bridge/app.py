"""
FastAPI app for bridge: GET /jobs (for SOVEREIGN_INGEST_URL), GET /health.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from sovereign_os.ingest_bridge.config import BridgeConfig
from sovereign_os.ingest_bridge.output import buffer_snapshot, buffer_take_all
from sovereign_os.ingest_bridge.runner import start_runner, stop_runner

logger = logging.getLogger(__name__)

_config: BridgeConfig | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _config
    _config = BridgeConfig.from_env()
    start_runner(_config)
    yield
    stop_runner()


app = FastAPI(title="Sovereign-OS Ingest Bridge", lifespan=lifespan)


@app.get("/jobs")
def get_jobs(take: bool = False):
    """
    Return pending jobs for SOVEREIGN_INGEST_URL.
    Format: array of {goal, amount_cents, currency, charter} or {jobs: [...]}.
    If take=true, return and clear buffer (consumed by poller).
    """
    if take:
        jobs = buffer_take_all()
    else:
        jobs = buffer_snapshot()
    return JSONResponse(content=jobs)


@app.get("/health")
def health():
    return {"status": "ok", "service": "ingest_bridge"}
