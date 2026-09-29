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

## It replicates, and it is not planner noise

Three independent runs of the full suite (14 goals completed in all three):

| Tier | run 1 | run 2 | run 3 | mean |
|---|---|---|---|---|
| small | 0.55 | 0.53 | 0.55 | **0.55** |
| medium | 0.87 | 0.97 | 1.07 | 0.97 |
| large | 1.67 | 1.57 | 1.73 | **1.66** |

The obvious objection to the single-run result was that realized cost depends on how the
planner happens to decompose a goal, so the "estimator error" might be decomposition
noise. Repeats settle it by separating the two:

| Component | sd (log) |
|---|---|
| within-task, run to run — planner and execution noise | 0.152 |
| between-task — systematic estimator error | **0.644** |

**95% of the variance is systematic.** The systematic component exceeds run-to-run noise
by 4.2x in standard deviation, so the slope is a property of the estimator, not an artifact
of a variable planner.

## The fix the system already had

The planner produces a task graph — task count, dependency structure, and a per-task
`estimated_token_budget` — after decomposition but **before execution**, which is exactly
where a budget ceiling belongs. The system computed it and then priced the mission from the
goal string anyway.

`estimate_plan_cost_cents` prices a plan task by task; `plan_complexity` derives difficulty
from task count and chain depth. Both are wired into the harness, which now records the
stage-1 and stage-2 predictions against the same realized cost.

### Measured head to head

Run 3 carried both predictions against the same realized cost (16 goals):

| | stage 1 (goal string) | stage 2 (plan-derived) |
|---|---|---|
| spread of tier means — *the slope* | 3.78x | **1.78x** |
| scatter (sd of log ratio) | 0.745 | **0.397** |
| level bias | 0.93x | **0.50x** |
| within 2x, after correcting the level | 62% | **94%** |

Stage 2 does the thing stage 1 could not: it more than halves the tier dependence and cuts
scatter 47%. It also introduces a level bias — the planner's own token budgets over-ask by
about 2x, consistently.

That division of labour is the useful part. **A level bias is exactly what a per-category
multiplier can remove**, and removing it takes accuracy from 62% to 94% within 2x. So the
conclusion is not that the calibration loop was wrong. The loop was fine; it was being fed
a flat signal. The two mechanisms are complementary:

- the plan-derived estimate corrects the **slope**, which a single multiplier structurally
  cannot;
- the existing `cost_factor` corrects the **level**, which the plan-derived estimate does
  not.

The planner produced between 2 and 16 tasks across the suite — an 8x dynamic range, which
is the responsiveness the goal-string score never had.

The honest caveat stands: `estimated_token_budget` is LLM-produced and inherits the error
under study, and its 2x over-ask is itself evidence of that. The claim is only that a
signal formed *after* decomposition tracks the work better than the goal's prose, and the
table above is what that is worth.

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

- **Three runs, one model, 14-17 goals each.** Enough to show the slope replicates and to
  separate it from planner noise; not enough for confidence intervals on individual
  per-tier figures, and all of it on `claude-haiku-4-5`. Whether the slope has the same
  shape on a frontier model is untested.
- **Tier is a hand-assigned label**, not an independent measurement of difficulty. It is
  the experimenter's intent, which is a reasonable proxy and not the same thing.
- **The stage-2 comparison rests on a single run.** The variance decomposition covers
  stage 1 across three runs; only run 3 carried plan data, so the 3.78x -> 1.78x slope
  improvement has not itself been replicated.
- **The correction schemes were fitted in sample.** All three are optimistic; only their
  comparison is meaningful, and only because the better-performing one has fewer
  parameters.
- **One or two goals failed per run** (`res-s`, the simplest research goal, every time)
  with validation errors on the planner's `TaskPlan`. A planner that reliably fails on the
  easiest item in the suite is a separate robustness issue, not a costing one, but it
  should not be left alone.

## Reproducing

```bash
export ANTHROPIC_API_KEY=sk-ant-…
unset OPENAI_API_KEY
SOVEREIGN_LLM_MODEL=claude-haiku-4-5-20251001 \
  python -m sovereign_os.bench.calibration_run --out data/calibration.jsonl
```

A fresh ledger needs seeding or the budget gate denies every task before it runs; the CLI
does this via `--budget-cents`.
