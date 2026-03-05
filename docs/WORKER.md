# How to add a Worker

Workers are agents that execute tasks. The CEO’s plan specifies a `required_skill`; the `WorkerRegistry` maps that skill to a concrete Worker class.

## 1. Implement a Worker

Subclass `BaseWorker` and implement `run` (and optionally `get_bid` for auctions):

```python
from sovereign_os.agents.base import BaseWorker, TaskInput, TaskResult

class MyWorker(BaseWorker):
    async def run(self, task_input: TaskInput) -> TaskResult:
        # Use task_input.task_id, .description, .context, etc.
        output = f"Done: {task_input.description[:50]}"
        return TaskResult(
            task_id=task_input.task_id,
            success=True,
            output=output,
        )
```

If you use the bidding/auction flow, implement `get_bid` to return estimated cost, time, and confidence.

## 2. Register the Worker

Before running missions, register your Worker for a skill name that matches your Charter’s `core_competencies[].name`:

```python
from sovereign_os.agents.registry import WorkerRegistry
from sovereign_os.models.charter import Charter

charter = load_charter("charter.example.yaml")
registry = WorkerRegistry(charter)
registry.set_default(StubWorker)  # fallback
registry.register("research", MyWorker)

# Pass registry to GovernanceEngine
engine = GovernanceEngine(charter, ledger, registry=registry, ...)
```

Now any task with `required_skill="research"` will be dispatched to `MyWorker`.

## 3. Worker contract

- **TaskInput** — `task_id`, `description`, `required_skill`, `context` (e.g. tool names, charter summary).
- **TaskResult** — `task_id`, `success` (bool), `output` (str). The Auditor evaluates `output` against the Charter’s KPIs.
- **System prompt** — The registry builds a prompt from the Charter (mission, competencies) and injects it when instantiating the Worker. Your Worker can use `self.system_prompt` if exposed by the base.

## 4. MCP and LLM

Workers can receive an optional `llm` (ChatLLM) and MCP tools. The registry can inject an LLM client per skill via `create_llm_client("worker_<skill>")`. See `sovereign_os.agents.registry` and `sovereign_os.llm.providers` for wiring.

## 5. StubWorker

For demos or when no real implementation exists, `StubWorker` returns a fixed success and short output. Use it as the default so unknown skills don’t crash the engine.
