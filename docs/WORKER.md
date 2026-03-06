# How to add a Worker

Workers are agents that execute tasks. The CEO’s plan specifies a `required_skill`; the `WorkerRegistry` maps that skill to a concrete Worker class.

## 1. Implement a Worker

Subclass `BaseWorker` and implement `execute` (and optionally `get_bid` for auctions):

```python
from sovereign_os.agents.base import BaseWorker, TaskInput, TaskResult

class MyWorker(BaseWorker):
    async def execute(self, task: TaskInput) -> TaskResult:
        # Use task.task_id, task.description, task.context, etc.
        output = f"Done: {task.description[:50]}"
        return TaskResult(
            task_id=task.task_id,
            success=True,
            output=output,
        )
```

If you use the bidding/auction flow, implement `get_bid` to return estimated cost, time, and confidence.

### Real example: SummarizerWorker

`sovereign_os.agents.summarizer_worker.SummarizerWorker` uses an LLM to summarize the task description. The registry injects `llm` when available; without it, the worker returns a short echo. Register it for a skill (e.g. `"research"` or `"summary"`) and ensure your Charter lists that competency:

```python
from sovereign_os.agents import WorkerRegistry
from sovereign_os.agents.summarizer_worker import SummarizerWorker

registry = WorkerRegistry(charter)
registry.set_default(StubWorker)
registry.register("research", SummarizerWorker)
# Now tasks requiring "research" use SummarizerWorker (LLM) instead of StubWorker.
```

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

## 5. StubWorker and SummarizerWorker

- **StubWorker** — For demos or when no real implementation exists; returns a fixed success and short output. Use it as the default so unknown skills don’t crash the engine.
- **SummarizerWorker** — Example of a real worker that calls the LLM to summarize the task. See `sovereign_os.agents.summarizer_worker`.

## 6. Built-in Workers (out of the box)

When using `charter.default.yaml` (Web UI default), the engine’s default registry includes common, general-purpose workers:

- **summarize** → `SummarizerWorker`
- **research** → `ResearchWorker`
- **reply** → `ReplyWorker`
- **write_article** → `ArticleWriterWorker`
- **solve_problem** → `ProblemSolverWorker`
- **write_email** → `EmailWriterWorker`
- **write_post** → `SocialPostWorker`
- **meeting_minutes** → `MeetingMinutesWorker`
- **translate** → `TranslateWorker`
- **rewrite_polish** → `RewritePolishWorker`
- **collect_info** → `InfoCollectorWorker`
- **extract_structured** → `ExtractStructuredWorker`
- **spec_writer** → `SpecWriterWorker`
- **assistant_chat** → `AssistantChatWorker` (generic Q&A when goal doesn’t match a specific skill)
- **code_assistant** → `CodeAssistantWorker` (understand code, suggest changes; analysis only)
- **code_review** → `CodeReviewWorker` (review code for issues and style; output only)

Each built-in worker reads `task.description` and optional `task.context` keys (tone, language, platform, schema, etc.) and returns a deliverable in Markdown or JSON-friendly text.
