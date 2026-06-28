# Category-driven delivery architecture

A single backbone — the **task category** — ties together worker routing, budget
ceilings, permission tiers, and connector needs, starting from the categories the
real marketplaces emit (BotBounty: code/research/creative/data/automation; TaskBounty/
StacksTasker: coding; ClawTasks: writing; RentAHuman: physical).

## The backbone — `agents/categories.py`

Each delivery category declares everything downstream systems need:

| Category | Skill (worker) | Risk | Budget | Capability | Connectors |
|---|---|---|---|---|---|
| coding | code_assistant | medium | $2.00 | WRITE_FILES | git, file_read, code_search |
| data | data_analysis | medium | $1.50 | WRITE_FILES | sql, spreadsheet, web_fetch |
| design | design_brief | medium | $1.50 | WRITE_FILES | figma, image_gen |
| email | write_email | medium | $0.75 | CALL_EXTERNAL_API | send_email |
| research | research | low | $1.00 | CALL_EXTERNAL_API | web_search, web_fetch |
| writing | write_article | low | $1.00 | READ_FILES | — |
| automation | spec_writer | high | $2.50 | EXECUTE_SHELL | workflow, webhook |
| general | assistant_chat | low | $0.50 | READ_FILES | — |

`categorize(platform_category, text)` classifies a task (platform label first,
then keywords); `route_skill(...)` returns the worker; `category_for_skill(...)`
is the reverse lookup used by budget/permission.

## Top-tier workers — `agents/specialist_workers.py`

The platform categories that lacked a strong worker now have one:
- **DesignBriefWorker** (`design_brief`) — build-ready design spec: IA, component
  states, tokens, layout, copy, handoff notes.
- **DataAnalysisWorker** (`data_analysis`) — reproducible analysis: assumptions,
  method, runnable SQL/pandas, tables, caveats; never invents numbers.

Both registered in the engine's default registry alongside the existing 16 workers.

## Budget redesign — `governance/budget_policy.py`

`CategoryBudgetPolicy` sets a per-task ceiling by **category × risk multiplier ×
global scale** (medium ×1.5, high ×2.0), so the CFO allocates more to high-value
categories and clamps low-value ones. `Treasury(charter, ledger,
budget_policy=...)` enforces the category ceiling in `approve_task(skill=...)` —
the tighter of the flat `max_task_cost_usd` cap and the category ceiling wins.
Backward compatible: no policy ⇒ unchanged.

## Permission redesign — per-category trust (`agents/auth.py`)

Trust is now earned **per delivery domain**. `record_audit(agent_id, passed=,
score=, category=)` accrues both global and per-category trust;
`effective_trust(agent, category)` uses the category score (seeded from global).
`check_permission_for(agent, capability, category)` and
`max_spend_cents_for(agent, category)` gate by domain — an agent proven at
writing gets a larger writing budget than at coding. Persisted with the rest of
the trust state.

## Connectors / MCP — `connectors/registry.py`

Each category declares the tools it needs. The registry catalogs every connector
(`kind`: mcp / builtin / http), its required env, and an MCP-server hint, and
reports readiness: `connectors_for_category`, `readiness_for_category`,
`required_mcp_servers`, `coverage_report`. MCP-kind connectors are fulfilled
through the existing self-hiring path (an `mcp-{skill}` worker backed by the
`MCPToolGraph`); this registry is the catalog + readiness layer on top.

## Demo

```bash
python examples/category_demo.py
```

Prints each sample platform task → category → skill → risk → budget → capability →
connector readiness, then shows permission being earned per category.
