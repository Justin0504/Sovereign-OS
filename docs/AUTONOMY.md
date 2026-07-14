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

## 1. CEO task selection — which jobs to take, on expected value

Choosing work is not just "does payout beat cost?" — it's **expected value**: the
payout weighted by how likely we are to actually deliver it, net of the platform's
real settlement economics. `sovereign_os/governance/opportunity.py` composes three
signals into one verdict:

1. **Platform economics** (`platform_economics`) — each rail's true settlement fee,
   gas, and currency (x402/Base ≈ free + a few cents gas; Stacks a touch more; fiat
   escrow ≈ 2.9% + 30¢). So net payout is computed correctly *per platform*. Extend or
   correct the table with `SOVEREIGN_PLATFORM_ECON_JSON` — no code change.
2. **Success probability** (`success_probability`) — our audit pass/fail track record
   in the task's category, Beta-smoothed (a new category starts from a modest prior and
   moves with evidence). This is where **delivery quality feeds task selection**: work
   we deliver reliably outranks nominally-higher-payout work we tend to fail.
3. **Cost** — the fully-loaded LLM estimate (`economics.py`).

`EV = P(success) · (payout − fee − gas) − LLM cost`. Take iff EV > 0 and the
success-case margin clears the floor.

```bash
SOVEREIGN_PROFIT_SCREEN=true   # ingest: drop negative-EV jobs before any compute
SOVEREIGN_EV_GATE=true         # auto-approve: CEO auto-declines negative-EV jobs (leaves pending)
SOVEREIGN_MIN_MARGIN_RATIO=0.3 # require >= 30% success-case margin
```

With `SOVEREIGN_PROFIT_SCREEN`, `sovereign_tasks_screened_total{decision="skip"}`
climbs for work that isn't worth taking. With `SOVEREIGN_EV_GATE`, an unattended
agent that has `SOVEREIGN_AUTO_APPROVE_JOBS` on will still leave low-EV jobs pending
rather than burn compute on them — using the whole team's per-category track record
(`category_history_all`). The CFO's `min_job_margin_ratio` remains a second line of
defense at mission start (see [CEO_CFO_PROFITABILITY.md](CEO_CFO_PROFITABILITY.md)).

## 1b. Profit maximization — thin margins, most total money

Maximizing per-job *margin* leaves money on the table; the objective is **total
profit under a compute budget**. That's a knapsack, and its budget-optimal ordering is
by **profit density** (expected profit ÷ compute cost). So a pile of thin, cheap jobs
beats a few fat expensive ones — "最小利润赚最高的钱." `governance/portfolio.py`:

- `select_portfolio(items, budget_cents)` picks the profit-maximizing set that fits the
  budget (greedy by density; take every positive-EV job when unconstrained). Set the
  margin floor to ~0 (`SOVEREIGN_MIN_MARGIN_RATIO=0`) to run pure volume mode.
- **Reward loop** (`YieldTracker`): every settled job's realized profit is attributed to
  its category×platform lane. Each lane's *yield* (profit per $ of compute) becomes a
  bounded EV multiplier (±25%) that the EV brain applies automatically — proven lanes
  rank higher, money-losing lanes back off. Earn → learn → earn more. Unseen lanes stay
  neutral, so the loop only acts on evidence, and it never flips a job's take/skip sign.
- **See the P&L**: `GET /api/finance` returns realized profit, spend, and ROI per lane,
  each lane's multiplier, and the most profitable lanes — honest attribution of where
  the money comes from.

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

For coding specifically, quality is enforced *before* the audit even sees the work:
the coding worker uses a **verification-driven loop** (`run_with_verified_tools`) that
will not accept a final answer until the repo's **test suite passes**. A premature
"done" is rejected with the failing test output and the model must fix and re-verify.
Code that can't reach green is marked `tests_verified=false`/`success=false`. Enforced
when `SOVEREIGN_CODE_EXEC_ENABLED=true` (sandboxed execution); a no-op skip otherwise.

## 3. Human-out-of-loop switches

| Env flag | Effect | Default |
|---|---|---|
| `SOVEREIGN_AUTO_APPROVE_JOBS=true` | Auto-approve ingested jobs (no manual approve) | off |
| `SOVEREIGN_COMPLIANCE_AUTO_PROCEED=true` | Skip human approval for high spend | off |
| `SOVEREIGN_PROFIT_SCREEN=true` | Drop negative-EV tasks at ingest | off |
| `SOVEREIGN_EV_GATE=true` | CEO auto-declines negative-EV jobs on auto-approve | off |
| `SOVEREIGN_MAX_REPAIR_ATTEMPTS=N` | Auto-repair failed tasks | 0 |
| `SOVEREIGN_OVERSIGHT_POLL_ENABLED=true` | Autonomous escrow settlement polling | off |
| `SOVEREIGN_SESSION_CEILING_CENTS=N` | CFO circuit-breaker session cap (safety net) | 0 (off) |
| `CLAWTASKS_LIVE` / `TASKBOUNTY_LIVE` / `STACKSTASKER_LIVE` / `APB_LIVE` | Real platform submission (else dry-run) | off |

**Always keep a safety net on** when running unattended: set a circuit-breaker
ceiling and/or `SOVEREIGN_MAX_CONSECUTIVE_FAILURES` so a bad run halts itself
(see [METRICS.md](METRICS.md) and the dashboard Guardrails tab).

## 4. Platform notes (2026)

- **x402 / USDC on Base** is the dominant agent-payment rail (Visa, Google, AWS,
  Stripe, Coinbase in the x402 Foundation). Sovereign-OS settles via the x402
  service (`payments/x402.py`, sandbox by default; run the go-live preflight before
  `X402_SANDBOX=false`).
- **APB (Agent Payment Bounty)** — the machine-readable x402 bounty format,
  published at `/.well-known/bounties.json`. The `APBOrderSource`
  (`ingest_bridge/sources/apb.py`) crawls publishers, parses each bounty's
  action/reward/network/claim (tolerant of field-name variants; amounts normalized
  to cents, atomic-aware via `decimals`), and emits jobs — the highest-growth
  autonomous discovery surface. Enable it:

  ```bash
  BRIDGE_APB_ENABLED=true
  APB_PUBLISHERS=https://pub-a.example,https://pub-b.example   # serve bounties.json
  APB_MIN_AMOUNT_USD=1          # optional payout floor
  ```

  Discovery is read-only. The last mile — submitting the finished work to the
  bounty's claim endpoint — is handled by `delivery/apb.py`, tolerant of the `claim`
  field being a URL, an object with a URL, or prose steps (no auto-submit). It is
  dry-run unless `APB_LIVE=true` (optional `APB_API_KEY` bearer); the reward itself
  settles over x402/USDC to the bounty's `payTo` when the publisher verifies — the
  adapter submits the result, it does not move funds. Full loop: **APB discover →
  profit screen → govern → execute → audit + self-repair → APB submit → x402 reward.**
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
