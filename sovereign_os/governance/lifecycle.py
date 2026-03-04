"""
TaskLifecycleManager: Real-time state for DAG tasks (PENDING, RUNNING, COMPLETED, FAILED).
Structured JSON logging for every transition to support debugging of parallel flows.
"""

import json
import logging
import threading
import time
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class TaskState(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


def _structured_log(
    event: str,
    task_id: str,
    state: TaskState,
    *,
    previous_state: TaskState | None = None,
    agent_id: str | None = None,
    error: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Emit a single JSON log line for task transition (for aggregation/debugging)."""
    payload: dict[str, Any] = {
        "event": event,
        "task_id": task_id,
        "state": state.value,
        "timestamp": time.time(),
    }
    if previous_state is not None:
        payload["previous_state"] = previous_state.value
    if agent_id is not None:
        payload["agent_id"] = agent_id
    if error is not None:
        payload["error"] = error
    if extra:
        payload.update(extra)
    logger.info("%s", json.dumps(payload, default=str))


class TaskLifecycleManager:
    """
    Tracks per-task state (PENDING -> RUNNING -> COMPLETED | FAILED).
    Thread-safe; emits structured JSON on every transition.
    """

    def __init__(self, task_ids: list[str]) -> None:
        self._lock = threading.Lock()
        self._state: dict[str, TaskState] = {tid: TaskState.PENDING for tid in task_ids}

    def get_state(self, task_id: str) -> TaskState:
        with self._lock:
            return self._state.get(task_id, TaskState.PENDING)

    def set_running(self, task_id: str, *, agent_id: str | None = None) -> None:
        with self._lock:
            prev = self._state.get(task_id, TaskState.PENDING)
            self._state[task_id] = TaskState.RUNNING
        _structured_log(
            "task_transition",
            task_id,
            TaskState.RUNNING,
            previous_state=prev,
            agent_id=agent_id,
        )

    def set_completed(self, task_id: str, *, agent_id: str | None = None, success: bool = True) -> None:
        with self._lock:
            prev = self._state.get(task_id, TaskState.RUNNING)
            self._state[task_id] = TaskState.COMPLETED
        _structured_log(
            "task_transition",
            task_id,
            TaskState.COMPLETED,
            previous_state=prev,
            agent_id=agent_id,
            extra={"success": success},
        )

    def set_failed(self, task_id: str, *, agent_id: str | None = None, error: str | None = None) -> None:
        with self._lock:
            prev = self._state.get(task_id, TaskState.RUNNING)
            self._state[task_id] = TaskState.FAILED
        _structured_log(
            "task_transition",
            task_id,
            TaskState.FAILED,
            previous_state=prev,
            agent_id=agent_id,
            error=error,
        )

    def completed_ids(self) -> set[str]:
        with self._lock:
            return {tid for tid, s in self._state.items() if s == TaskState.COMPLETED}

    def all_done(self) -> bool:
        with self._lock:
            return all(s in (TaskState.COMPLETED, TaskState.FAILED) for s in self._state.values())

    def snapshot(self) -> dict[str, str]:
        with self._lock:
            return {tid: s.value for tid, s in self._state.items()}
