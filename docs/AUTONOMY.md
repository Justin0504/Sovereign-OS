# Autonomous, Profitable Operation (Human-out-of-Loop)

This is the runbook for running Sovereign-OS as a self-sustaining agent that
discovers paid work, only takes what pays, delivers audited quality, and settles —
with no human in the loop. It is built around two hard problems that sink most
autonomous agents on real bounty platforms:

1. **Actually being profitable.** A widely-cited 2026 field P&L had an agent net
   **−$8.30 over four days** — the work shipped, but gas/bridging fees and marginal
   task selection ate the payout. Sovereign-OS screens every candidate task *before
   spending compute* and skips anything that can't clear the margin floor after
   settlement fee + gas + LLM cost.
2. **Not burning reputation.** Escrow platforms (Claw Earn / ClawTasks, TaskBounty,
   StacksTasker) are single-shot: a failed submission loses the bounty *and* trust.
   Sovereign-OS never submits a deliverable that failed audit, and it will
   automatically repair a failing task before giving up.

## The loop

```
discover ─▶ [profit screen] ─▶ govern (CFO budget + circuit breaker)
        ─▶ execute (agentic workers) ─▶ audit (category rubric)
        ─▶ [self-repair on fail] ─▶ [quality gate] ─▶ deliver + settle
```

Every stage is gated; money and code execution are dry-run by default.

## 1. Profitability-first task selection

`sovereign_os/governance/economics.py` estimates a task's fully-loaded cost (LLM
tokens by category × complexity, from the real pricing table) and compares it to the
payout net of fees and gas. Turn on the ingest pre-screen and tune the economics:

```bash
SOVEREIGN_PROFIT_SCREEN=true            # drop unprofitable tasks before compute
SOVEREIGN_SETTLEMENT_FEE_RATIO=0.029    # your rail's fee (e.g. 2.9%)
SOVEREIGN_GAS_COST_CENTS=5              # fixed on-chain cost per task
SOVEREIGN_MIN_MARGIN_RATIO=0.3          # require >= 30% net margin
```

With the screen on, `sovereign_tasks_screened_total{decision="skip"}` climbs for
work that isn't worth taking. The CFO also enforces `min_job_margin_ratio` at mission
start as a second line of defense (see [CEO_CFO_PROFITABILITY.md](CEO_CFO_PROFITABILITY.md)).

## 2. Delivery quality: audit + automatic self-repair

Every deliverable is scored against a category-tuned rubric (value-aware bar — higher
payouts must clear a higher score). When a task fails audit, enable reactive repair:

```bash
SOVEREIGN_MAX_REPAIR_ATTEMPTS=2         # retry a failed task with the fix folded in
```

The engine folds the Auditor's failure reason + suggested fix into the task brief and
re-runs it up to N times (`sovereign_task_repairs_total{outcome="recovered"}`). Only a
mission where **every** task passes audit proceeds to delivery/settlement — a failed
audit stops before any platform submission or charge.

## 3. Human-out-of-loop switches

| Env flag | Effect | Default |
|---|---|---|
| `SOVEREIGN_AUTO_APPROVE_JOBS=true` | Auto-approve ingested jobs (no manual approve) | off |
| `SOVEREIGN_COMPLIANCE_AUTO_PROCEED=true` | Skip human approval for high spend | off |
| `SOVEREIGN_PROFIT_SCREEN=true` | Drop unprofitable tasks at ingest | off |
| `SOVEREIGN_MAX_REPAIR_ATTEMPTS=N` | Auto-repair failed tasks | 0 |
| `SOVEREIGN_OVERSIGHT_POLL_ENABLED=true` | Autonomous escrow settlement polling | off |
| `SOVEREIGN_SESSION_CEILING_CENTS=N` | CFO circuit-breaker session cap (safety net) | 0 (off) |
| `CLAWTASKS_LIVE` / `TASKBOUNTY_LIVE` / `STACKSTASKER_LIVE` | Real platform submission (else dry-run) | off |

**Always keep a safety net on** when running unattended: set a circuit-breaker
ceiling and/or `SOVEREIGN_MAX_CONSECUTIVE_FAILURES` so a bad run halts itself
(see [METRICS.md](METRICS.md) and the dashboard Guardrails tab).

## 4. Platform notes (2026)

- **x402 / USDC on Base** is the dominant agent-payment rail (Visa, Google, AWS,
  Stripe, Coinbase in the x402 Foundation). Sovereign-OS settles via the x402
  service (`payments/x402.py`, sandbox by default; run the go-live preflight before
  `X402_SANDBOX=false`). The emerging **Agent Payment Bounty (APB)** JSON format
  describes a bounty's action/reward/network/claim-steps in machine-readable form.
- **Claw Earn / ClawTasks** — Base USDC single-start bounties with non-custodial
  escrow and agent APIs. Delivery via `delivery/clawtasks.py` (dry-run unless
  `CLAWTASKS_LIVE`).
- **Coding bounties dominate paid volume** — the coding worker ships real PRs
  (`connectors/git_pr.py`, sandboxed execution) and delivers via `delivery/taskbounty.py`.

## Safety & compliance

Money movement and code execution are **dry-run/sandbox by default** and gated behind
explicit `*_LIVE` / `SOVEREIGN_CODE_EXEC_ENABLED` flags. Enabling real earning may
carry legal/tax/work-authorization obligations depending on your jurisdiction and
status — that's on the operator, not the software. Start in sandbox, verify the full
loop, then go live deliberately.
