# How well does the system predict what its own work will cost?

Every money decision in Sovereign-OS descends from one function. `estimate_task_cost_cents`
feeds the EV screen that decides which work to take, the bid floor that decides what to
charge, the portfolio selection that decides what fits the budget, and the lane allocator
that decides where tomorrow's budget goes. Until now nothing measured whether it was any
good.

This is the first measurement. Setup, results, what broke, and what the numbers do not
support.

## Setup

- 18 goals: six categories (coding, research, writing, data, design, automation) at three
  intended difficulty tiers. A corpus concentrated in one category would measure the
  category rather than the estimator.
- Executed end to end through the real governance engine on `claude-haiku-4-5`.
- 17 completed, 1 failed. Total real spend **$0.75**.

Two measurement decisions carry the result:

**The prediction is taken uncalibrated.** `calibrated=False`, so what is measured is the
raw estimator rather than one already corrected by the factor under evaluation. Otherwise
the experiment chases its own output.

**The realized cost is not self-reported.** It is read through `GroundTruthMonitor`'s tap
on `UnifiedLedger.record_*`, and re-priced from exact token counts. Models are poor at
introspecting their own token use, so an agent's account of what it spent is not evidence.

## The result: a slope error, not scatter

| Tier | n | Geometric-mean ratio | Reading |
|---|---|---|---|
| small | 5 | **0.55** | over-estimated ~1.8x |
| medium | 6 | **0.84** | slightly over-estimated |
| large | 6 | **1.67** | **under-estimated ~1.7x** |

Monotonic across tiers, spanning 3x, in a consistent direction. The estimator is not
noisy — it is tilted.

## Why

```
complexity score   small → large:  0.78 → 0.94   (1.21x)
resulting estimate small → large:  3.8¢ → 4.3¢   (1.14x)
realized cost      small → large:  2.21¢ → 7.31¢ (3.31x)
realized cost across the whole suite:            12.8x
```

`complexity_from_goal` is a function of **goal string length**. The most expensive task in
the suite — refactoring a client to async with retries, pooling, timeouts and a full test
suite — is about 150 characters and cost 13.47¢, 12.8x the cheapest. Prose length does not
track work volume, so the estimator stays flat while reality climbs.

## The deployed correction cannot fix it

`cost_factor` learns one multiplier per category. A single multiplier shifts the line; it
cannot change the slope. The within-category spread is the whole problem:

| Category | small | medium | large | one factor must cover |
|---|---|---|---|---|
| coding | 0.43 | 0.39 | 1.68 | 4.3x |
| writing | 0.35 | 0.80 | 1.45 | 4.1x |
| data | 0.36 | 0.77 | 1.33 | 3.7x |
| automation | 0.98 | 1.56 | 2.92 | 3.0x |

Fitting the best possible correction of each kind, in sample:

| Scheme | Free params | Residual sd | within 2x | Error reduction |
|---|---|---|---|---|
| raw estimator | 0 | 0.601 | 71% | — |
| per-category *(deployed)* | 6 | 0.487 | 82% | 19% |
| **per-tier** *(a difficulty signal)* | **3** | **0.393** | **94%** | **35%** |

Conditioning on difficulty beats conditioning on category while using half the parameters.
More parameters doing less work is what rules out the alternative explanation that this is
fitted noise.

## The fix the system already had

The planner produces a task graph — task count, dependency structure, and a per-task
`estimated_token_budget` — after decomposition but **before execution**, which is exactly
where a budget ceiling belongs. The system computed it and then priced the mission from the
goal string anyway.

`estimate_plan_cost_cents` prices a plan task by task; `plan_complexity` derives difficulty
from task count and chain depth. Both are wired into the harness, which now records the
stage-1 and stage-2 predictions against the same realized cost.

The honest caveat: `estimated_token_budget` is LLM-produced and inherits the error under
study. The only claim is that it is formed after the model has actually decomposed the
problem, so it rests on strictly more information than the goal string. Whether that
converts into a better estimate is for the next run to answer, not this document.

## Bugs this surfaced

Running against the real API found three defects that no unit test had, because they only
appear when real money and real model ids are involved.

1. **The default Anthropic model was retired.** `_default_model("anthropic")` returned
   `claude-sonnet-4-20250514`, which the API now 404s. Every LLM call failed for any
   deployment carrying only an Anthropic key.
2. **Opus 4.5+ was priced at 3x its real cost.** The pricing table's `claude-opus-4` entry
   at the retired $15/$75 rate is a *prefix* of `claude-opus-4-5` through `-4-8`, which
   actually cost $5/$25. Longest-prefix matching therefore over-costed every modern Opus
   call — and a 3x cost overestimate makes the EV screen silently reject profitable work.
3. **Integer-cent quantization distorted per-task attribution.** 42% of individual calls
   rounded to zero cents. In aggregate the errors nearly cancel (-0.2% over 19 calls), so
   the ledger's totals are sound — this was *not* the systematic under-count it first
   appeared to be. But a single task makes only a handful of calls, and correcting to full
   precision moved the measured result materially: geometric-mean ratio 0.451 → 0.684, and
   the overrun rate 0.00 → 0.33. Rounding had been hiding that a third of tasks ran over.

## What these numbers do not support

- **n = 17, one model, one run.** No repeats, so within-task variance is unknown and the
  per-tier figures carry no confidence interval. Repeat runs are what make or break the
  headline.
- **Tier is a hand-assigned label**, not an independent measurement of difficulty. It is
  the experimenter's intent, which is a reasonable proxy and not the same thing.
- **Realized cost depends on the planner, not only the estimator.** How many subtasks a
  goal decomposes into varies run to run, so some of the measured "estimator error" is
  planner variance. Separating the two needs repeats; until then the slope result should
  be read as *the pair* being mis-sloped.
- **The correction schemes were fitted in sample.** All three are optimistic; only their
  comparison is meaningful, and only because the better-performing one has fewer
  parameters.
- **One task failed** (`res-s`, the simplest research goal) with three validation errors
  on the planner's `TaskPlan`. That is a separate robustness issue, not a costing one.

## Reproducing

```bash
export ANTHROPIC_API_KEY=sk-ant-…
unset OPENAI_API_KEY
SOVEREIGN_LLM_MODEL=claude-haiku-4-5-20251001 \
  python -m sovereign_os.bench.calibration_run --out data/calibration.jsonl
```

A fresh ledger needs seeding or the budget gate denies every task before it runs; the CLI
does this via `--budget-cents`.
