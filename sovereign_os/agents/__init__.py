"""
Agent runtime: BaseWorker, WorkerRegistry, and SovereignAuth.
"""

from sovereign_os.agents.auth import Capability, PermissionDeniedError, SovereignAuth
from sovereign_os.agents.base import BaseWorker, StubWorker, TaskInput, TaskResult
from sovereign_os.agents.registry import WorkerRegistry

__all__ = [
    "BaseWorker",
    "Capability",
    "PermissionDeniedError",
    "SovereignAuth",
    "StubWorker",
    "TaskInput",
    "TaskResult",
    "WorkerRegistry",
]
