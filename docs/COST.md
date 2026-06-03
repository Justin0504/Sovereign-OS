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

## 3. Estimate → actual overrun loop

The CFO's pre-task estimate is stored per task. After execution,
`GovernanceEngine._reconcile_cost` compares actual token cost to that estimate;
if it exceeds it by more than `BUDGET_OVERRUN_TOLERANCE` (default 25%), the agent
is docked via `SovereignAuth.record_budget_overrun` and a `budget_overrun` event
is emitted. Chronic over-spenders lose TrustScore — and therefore their graduated
autonomous spend ceiling — over time.

## 4. Hard per-task ceiling (`max_task_cost_usd`)

`FiscalBoundaries.max_task_cost_usd` (default 0 = off) is a hard pre-flight cap:
`Treasury.approve_task` rejects any single task whose estimated cost exceeds it,
regardless of available balance. (Fills the `max_task_cost_usd` guarantee the
README already advertised.)

## 5. Cost trace surfaces

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

All changes are backward compatible: new charter fields default to disabled, and
the existing test suite passes unchanged.
