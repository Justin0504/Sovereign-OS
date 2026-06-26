# ClawTasks Integration (auto-accept loop)

Pull open bounties from the [ClawTasks](https://clawtasks.com) agent-to-agent
marketplace into the Sovereign-OS job queue, let the governed workforce do the
work, and (optionally) submit the result back for USDC settlement on Base.

## The loop

```
ClawTasks /bounties ──▶ ClawTasksOrderSource ──▶ job queue ──▶ CEO→CFO→Workers→Auditor
                                                                      │
                          ClawTasks claim+submit ◀── delivery ◀───────┘
```

1. **Discovery** (`ingest_bridge/sources/clawtasks.py` → `ClawTasksOrderSource`)
   polls `GET /bounties?status=open`, keeps open + funded bounties not already
   assigned, and emits one job per bounty (`amount` → cents, `currency` USDC,
   `delivery_contact = {platform: clawtasks, bounty_id, mode}`). **No auth, no
   funds** — safe to run continuously.
2. **Execution** is the normal pipeline: plan → CFO budget → workers → audit.
3. **Delivery** (`delivery/clawtasks.py`) claims the bounty and submits the
   deliverable. Claiming stakes 10% of the bounty in USDC on Base — **money-moving**,
   so it runs **dry-run** (logs only) unless `CLAWTASKS_LIVE=true`.

## Enable

```bash
pip install -e ".[bridge]"          # provides requests
# Discovery only (safe):
export BRIDGE_CLAWTASKS_ENABLED=true
export CLAWTASKS_MIN_AMOUNT_USD=1
python -m sovereign_os.ingest_bridge   # serves jobs on :9000
# Point Sovereign-OS at it:
export SOVEREIGN_INGEST_URL=http://localhost:9000/jobs?take=true
export SOVEREIGN_INGEST_ENABLED=true SOVEREIGN_JOB_WORKER_ENABLED=true
```

To submit results back (real USDC), add `CLAWTASKS_API_KEY` and
`CLAWTASKS_LIVE=true` with a funded Base wallet. See `.env.example`.

## Aligning to other platforms

`ClawTasksOrderSource` takes an injectable `get_json` and maps a standard
bounty shape (`id, title, description, amount, currency, status, funded,
assigned_to, tags`). Any marketplace exposing a compatible JSON list — e.g.
TaskBounty — can reuse it by subclassing and overriding `_list_open_bounties`.

## Reality check (verified 2026-06)

The integration is aligned to ClawTasks' documented REST API and validated
against the live `GET /api/config` endpoint (returns the Base contract/USDC
addresses, `chain_id 8453`, `stake_percent 10`). However, at time of writing the
platform's read endpoints (`/bounties`, `/feed`, `/leaderboard`) return HTTP 500
and `config` reports `free_tasks_only: true` ("simplifying to free tasks while we
harden reliability"). The source handles this gracefully — it logs the upstream
error and emits zero jobs rather than crashing the loop. When ClawTasks restores
`/bounties` (or a compatible feed is configured), real tasks flow in with no code
change. This early-stage instability is typical of the current agent-task
marketplace ecosystem.
