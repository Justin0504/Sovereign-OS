# Cost Tracing & Control

Accurate, per-model cost accounting plus a closed estimate→actual control loop.

## Why

The ledger previously costed every model at one blended rate (~10 cents / 1k
tokens), so a `gpt-4o-mini` call and an `o1` call recorded the same cost despite
a ~100x real price gap, the cost of **failed** tasks was dropped entirely, and
the `record_budget_overrun` penalty was never actually triggered. This makes cost
traces real and wires the control loop.

## 1. Per-model pricing (`sovereign_os/governance/pricing.py`)

`estimate_cost_cents(model_id, input_tokens, output_tokens)` prices input and
output tokens separately, per model (USD per 1M tokens), with:

- exact match → longest-prefix match (`gpt-4o-2024-11-20` → `gpt-4o`, but
  `gpt-4o-mini` stays distinct) → conservative fallback (never silently $0).
- runtime override/extension via `SOVEREIGN_MODEL_PRICING_JSON` (see `.env.example`).
- `estimate_cost_usd(...)` for sub-cent precision (cent rounding loses sub-cent calls).

The engine uses this for every task whose worker reports token usage.

## 2. Cost leak fix — failed tasks are costed too

Token cost is now recorded whenever usage is reported, **including failed tasks**
(they still burn tokens). Previously recording was gated on `result.success`, so
failures were invisible in the P&L.

## 3. Pre-flight estimate shares the actual's basis

The CFO's per-task pre-flight estimate now prices the task's token budget with the
same per-model table the ledger uses for actuals (via `estimate_budget_cost_cents`
and `Treasury.get_optimal_model`, which returns real priced model ids —
`gpt-4o` for high priority, `gpt-4o-mini` for low, overridable with
`SOVEREIGN_COST_MODEL_HIGH` / `SOVEREIGN_COST_MODEL_LOW`). Previously the estimate
used a flat ~10¢/1k-token rate — ~20x over-budget for `gpt-4o` and blind to the
model — which made the overrun loop below fire almost never. Now estimate and
actual are comparable, so budgets and overruns are meaningful.

The token budget is also split into input/output by task type before pricing
(`output_ratio_for_skill`): output tokens cost ~4x input, so a generation-heavy
job (`write_article`, ratio 0.75) and an input-heavy one (`summarize`, ratio
0.25) at the same total budget estimate very differently.

## 4. Estimate → actual overrun loop

The CFO's pre-task estimate is stored per task. After execution,
`GovernanceEngine._reconcile_cost` compares actual token cost to that estimate;
if it exceeds it by more than `BUDGET_OVERRUN_TOLERANCE` (default 25%), the agent
is docked via `SovereignAuth.record_budget_overrun` and a `budget_overrun` event
is emitted. Chronic over-spenders lose TrustScore — and therefore their graduated
autonomous spend ceiling — over time.

## 5. Hard per-task ceiling (`max_task_cost_usd`)

`FiscalBoundaries.max_task_cost_usd` (default 0 = off) is a hard pre-flight cap:
`Treasury.approve_task` rejects any single task whose estimated cost exceeds it,
regardless of available balance. (Fills the `max_task_cost_usd` guarantee the
README already advertised.)

## 6. Budget-exhaustion stop (`max_mission_cost_usd`)

`FiscalBoundaries.max_mission_cost_usd` (default 0 = off) caps cumulative spend
for a single mission. `GovernanceEngine.dispatch` accumulates actual token spend
per task and, before each DAG wave, checks the cap; once reached it halts —
remaining tasks are marked `budget_halt` (not run) and a
`mission_budget_exhausted` event fires. The in-flight wave finishes (tasks are
not cancelled mid-call); only subsequent waves are stopped.

## 7. Cost trace surfaces

`UnifiedLedger` rollups:

- `cost_cents_by_model()` / `cost_cents_by_agent()` / `cost_cents_by_task()`
- `cost_summary()` — totals, token counts, and all three breakdowns.

The CLI prints a compact trace after every mission:

```
Cost: $0.0312 (4200 in + 1800 out tokens)
    gpt-4o: $0.0260
    gpt-4o-mini: $0.0052
  By agent:
    research: $0.0260
    writer: $0.0052
```

The web dashboard's **Cost breakdown** card (`GET /api/cost_summary`) shows the
same per-model/per-agent split plus a **daily-burn bar** — today's USD debits
against `daily_burn_max_usd`, turning red at ≥90% utilization.

All changes are backward compatible: new charter fields default to disabled, and
the existing test suite passes unchanged.
