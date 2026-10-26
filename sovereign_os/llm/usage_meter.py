"""
Token usage recorded where the API answered, not where the worker says it did.

The ledger used to price each task from `result.metadata` -- input tokens, output tokens
and model id, all returned by `worker.execute()`. That is the worker's own account of
what it spent. The threat model's defense against BE-04 (model substitution: declare a
cheap model, invoke an expensive one) was "cost is attributed from the model actually
invoked, recorded at the client layer below the agent's control", and the code did the
opposite. It is also a direct violation of R1: an invariant stated over a report.

The LLM clients already saw the real usage on every response, but kept only the last
call in `_last_usage` and nothing read it. This module gives them somewhere to put it:
a per-task accumulator carried in a ContextVar, opened by the engine around
`worker.execute()`, appended to by the client after every successful response. A worker
does not write it and cannot reach it except by making real calls.

What it does not cover: a worker that calls a provider through its own client rather
than through a ChatLLM, or that runs a model call in a thread without copying its
context. Those fall back to the worker's report, and the engine records which source
priced the task, so the gap is visible rather than silent.
"""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
from dataclasses import dataclass, field


@dataclass
class TaskUsage:
    """Usage per model actually invoked during one task."""

    by_model: dict[str, list[int]] = field(default_factory=dict)   # model -> [in, out]
    calls: int = 0

    def add(self, model: str, input_tokens: int, output_tokens: int) -> None:
        slot = self.by_model.setdefault(model or "default", [0, 0])
        slot[0] += max(0, int(input_tokens or 0))
        slot[1] += max(0, int(output_tokens or 0))
        self.calls += 1

    @property
    def input_tokens(self) -> int:
        return sum(v[0] for v in self.by_model.values())

    @property
    def output_tokens(self) -> int:
        return sum(v[1] for v in self.by_model.values())


_current: contextvars.ContextVar[TaskUsage | None] = contextvars.ContextVar(
    "_task_usage", default=None)


@contextmanager
def meter_task():
    """Open a fresh accumulator for the duration of one task's execution."""
    usage = TaskUsage()
    token = _current.set(usage)
    try:
        yield usage
    finally:
        _current.reset(token)


def record(model: str, input_tokens: int, output_tokens: int) -> None:
    """Called by an LLM client after a successful response. A no-op outside a task."""
    usage = _current.get()
    if usage is not None:
        usage.add(model, input_tokens, output_tokens)
