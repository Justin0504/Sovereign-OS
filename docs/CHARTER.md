# How to write a Charter

The Charter is the single source of truth for what your autonomous entity is and does. The system does not “know” the business until it parses a Charter (YAML).

## Schema

| Field | Type | Description |
|-------|------|-------------|
| `mission` | string | High-level mission statement (used by CEO/Strategist). |
| `core_competencies` | list | Capabilities the entity can hire workers for. |
| `fiscal_boundaries` | object | Daily burn cap, max budget, currency. |
| `success_kpis` | list | KPIs the Auditor uses to verify task outputs. |
| `entity_id` | string (optional) | Stable identifier for this charter instance. |

## Core competencies

Each competency has:

- **name** — Must match the `required_skill` in the CEO’s task plan (e.g. `research`, `code`).
- **description** — Shown to the Strategist and injected into Worker system prompts.
- **priority** — 1–10; used for bid scoring and model selection.

Example:

```yaml
core_competencies:
  - name: research
    description: Web and document research, synthesis, citation
    priority: 8
  - name: code
    description: Software development, testing, deployment
    priority: 9
```

## Fiscal boundaries

- **daily_burn_max_usd** — Max USD the CFO allows to be spent per calendar day.
- **max_budget_usd** — Total budget cap (runway ceiling).
- **currency** — e.g. `USD`.

The CFO denies any task that would exceed these limits (`FiscalInsolvencyError`).

## Success KPIs

Each KPI has:

- **name** — Identifier (e.g. `task_completion_rate`).
- **metric** — Metric name (e.g. `tasks_verified_ok`).
- **target_value** — Optional numeric target.
- **unit** — Optional (e.g. `ratio`, `cents`).
- **verification_prompt** — Text used by the Auditor (Judge LLM) to verify task output.

Example:

```yaml
success_kpis:
  - name: task_ok
    metric: tasks_verified_ok
    target_value: 0.95
    unit: ratio
    verification_prompt: "Did the output satisfy the task success criteria?"
```

## Loading a Charter

```python
from sovereign_os.models.charter import load_charter

charter = load_charter("path/to/your/charter.yaml")
```

Use this Charter when building the `GovernanceEngine`, `ReviewEngine`, and `WorkerRegistry` so the whole pipeline stays aligned with one constitution.
