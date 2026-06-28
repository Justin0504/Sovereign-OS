# Oversight: governance over outbound work

Sovereign-OS as the regulatory layer when an agent **posts** a task for an
external worker (human or another agent) to do — solving the two hard problems of
the agent-task economy: **budget control** and **delivery quality**.

```
post a task ─▶ [BUDGET GATE: CFO] ─▶ fund escrow ─▶ external worker delivers
                     │ reject if over budget                 │
                     └─ nothing funded            [QUALITY GATE: Auditor]
                                                   ├─ pass → complete + release (pay)
                                                   └─ fail → dispute (funds withheld)
```

This is the outbound complement to the inbound ingest sources
(ClawTasks/TaskBounty), where Sovereign-OS's own agents do the work. Same two
governance primitives, opposite direction.

## Components

- **`oversight/broker.py` — `OversightBroker`** (platform-agnostic):
  - `post_governed_task(...)` runs `Treasury.approve_task` (balance, daily cap,
    per-task ceiling `max_task_cost_usd`, runway floor) *before* posting/funding.
    Over-budget → returns `{posted: False, reason}`, nothing funded.
  - `review_and_settle(...)` runs `ReviewEngine.audit_task` on the deliverable
    (value-aware bar: higher-paid tasks need a higher score). Pass → `complete`
    + `release` and the spend is recorded in the ledger. Fail → `dispute`,
    funds withheld.
- **`oversight/rentahuman.py` — `RentAHumanClient`**: the
  [RentAHuman](https://rentahuman.ai) escrow API (`POST /bounties`,
  `/escrow/checkout`, `/escrow/:id/complete|release|dispute|cancel`).
  Money-moving calls are **dry-run** unless `live=True` (env `RENTAHUMAN_LIVE`),
  so the whole loop runs without an account or funds.

Any client implementing the `EscrowClient` protocol (post/fund/complete/release/
dispute/cancel) can be governed — e.g. agent-posts-bounty on ClawTasks/TaskBounty.

## Run the demo (no key, no network, no funds)

```bash
python examples/oversight_demo.py
```

Shows an $80 task rejected by the budget gate, a good deliverable released ($25
paid), and an empty deliverable disputed ($15 withheld).

## Going live

Set `RENTAHUMAN_API_KEY` (`rah_live_*`) and construct the client with `live=True`.
Funding escrow and releasing payment then move real money via the RentAHuman /
Stripe escrow — gate it behind your own confirmation.
