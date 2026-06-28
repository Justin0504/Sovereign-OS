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
dispute/cancel) can be governed.

- **`oversight/rentahuman.py`** — full escrow lifecycle (budget gate + quality
  gate + release/dispute). The reference outbound platform.
- **`oversight/stackstasker.py` — `StacksTaskerClient`** — StacksTasker is
  agent-to-agent, STX/testnet, settled on-chain via bids. So the **budget gate
  fully applies to posting** (`POST /tasks`), but there is no poster-controlled
  release/dispute escrow — those calls are logged no-ops and `get_escrow` reports
  `open` so the poller won't try to settle. Amounts are nominal STX, never USD.

Funds are reserved in the ledger at **funding time** (not release), so concurrent
posts can't over-commit the balance; release keeps the reservation, dispute
refunds it.

## Go-live preflight

Before flipping `RENTAHUMAN_LIVE=true`, run the safety check (verifies config and
the post/fund/release path WITHOUT moving funds, returns GO / NO-GO):

```bash
python -m sovereign_os.oversight.rentahuman_preflight
```

## Run the demo (no key, no network, no funds)

```bash
python examples/oversight_demo.py
```

Shows an $80 task rejected by the budget gate, a good deliverable released ($25
paid), and an empty deliverable disputed ($15 withheld).

## Auto-settle loop, registry, web panel, CLI

- **`oversight/registry.py` — `OversightRegistry`**: tracks every posted escrow
  (status `funded → delivered → released | disputed`, plus `rejected` when the
  budget gate blocks a post). Optional JSON persistence (`SOVEREIGN_OVERSIGHT_DB`).
- **`oversight/poller.py` — `poll_and_settle(broker, registry)`**: for each funded
  escrow whose platform status is `delivered`, runs the quality gate and
  releases or disputes — turning post→wait→verify→pay into a hands-off loop.
- **Web**: `GET /api/oversight` (escrows + status summary), `POST /api/oversight/hire`
  (budget-gated post), `POST /api/oversight/poll` (settle delivered), and an
  **Outbound escrows** dashboard card with status chips.
- **CLI**:
  - `sovereign hire --title "…" --price-cents 1500` — outbound, budget-gated (dry-run).
  - `sovereign pull taskbounty|stackstasker|clawtasks` — inbound, list live open tasks.

## Going live

Set `RENTAHUMAN_API_KEY` (`rah_live_*`) and `RENTAHUMAN_LIVE=true`.
Funding escrow and releasing payment then move real money via the RentAHuman /
Stripe escrow — gate it behind your own confirmation.
