# Demo Script: Auto Ingest → CEO/CFO → Permissions (Firewall) → Delivery → Stripe

This doc describes a **single, realistic end-to-end demo** that shows:

1. **Orders from the web** — Jobs arrive automatically (ingest from a URL or API).
2. **CEO** — Splits each order into a task plan (skills, token estimates).
3. **CFO** — Approves or denies budget per plan; balance and caps enforced.
4. **Dynamic agent permissions (firewall)** — Each task runs only if the agent’s TrustScore meets the required capability (e.g. SPEND_USD needs 80); audit pass/fail updates scores.
5. **Delivery** — Completed job triggers a webhook (your backend or a request bin); payload includes `result_summary`, `job_id`, `status`.
6. **Earn to Stripe** — On completion, Stripe charges the customer (test mode); Ledger records `job_income`.

All of this uses the **real** Sovereign-OS pipeline: no mocks for CEO, CFO, auth, or Stripe in the main path.

---

## How it fits together

| Step | What happens (real) | Where you see it |
|------|---------------------|------------------|
| 1. Ingest | A background poller hits `SOVEREIGN_INGEST_URL` and enqueues jobs from the JSON. | New rows in **Job queue** (pending → approved if auto-approve is on). |
| 2. CEO | Strategist turns the job `goal` into a `TaskPlan` (N tasks, skills, token estimate). | **Decision stream**: “CEO: Plan created — N tasks. Goal: …” |
| 3. CFO | Treasury checks balance and daily burn; approves or denies. | **Decision stream**: “CFO: Approved N task(s), est. $X. Balance: $Y.” or “CFO denied budget”. |
| 4. Firewall | Before each task, engine asks SovereignAuth: does this agent’s TrustScore ≥ threshold for the task’s capability? (e.g. summarizer → READ_FILES 10, LLM call → CALL_EXTERNAL_API 50.) | **Trust** in the top bar; “permission OK” in the stream; if denied, “permission_denied” and task fails. |
| 5. Run & audit | Workers run; Auditor checks output vs Charter KPIs; pass → TrustScore +5, fail → -15. | **Tasks** card; **Decision stream** audit lines; **Trust** value changes. |
| 6. Delivery | Job status → `completed` (or `payment_failed`); HTTP POST to `SOVEREIGN_WEBHOOK_URL` and/or job `callback_url`. | Your endpoint receives JSON (e.g. `result_summary`, `job_id`, `status`). |
| 7. Stripe | Payment service charges `amount_cents` (test card in test mode); Ledger appends `job_income`. | **Balance** in Dashboard; Ledger file; Stripe Dashboard (test). |

So: **auto from web** = ingest URL; **CEO/CFO** = real Strategist + Treasury; **firewall** = SovereignAuth + TrustScore; **delivery** = webhook; **earn** = Stripe + Ledger.

---

## Prerequisites

- `.env` with:
  - `STRIPE_API_KEY=sk_test_...` (Stripe test mode).
  - `OPENAI_API_KEY` or `ANTHROPIC_API_KEY`.
  - Optional: `SOVEREIGN_JOB_DB=./data/jobs.db`, `SOVEREIGN_LEDGER_PATH=./data/ledger.jsonl`.
  - For full auto flow: `SOVEREIGN_AUTO_APPROVE_JOBS=true`, `SOVEREIGN_COMPLIANCE_AUTO_PROCEED=true`.
  - For delivery demo: `SOVEREIGN_WEBHOOK_URL` (e.g. a request bin or your server).

---

## Option A: Ingest from a “web” URL (recommended for “auto from web”)

This simulates **orders coming from an external API** by serving a static JSON file locally; the app polls it like a real ingest endpoint.

### 1. Start a local server that serves “orders”

From the project root:

```powershell
# Windows (PowerShell) — serve examples/ on port 8888
cd Sovereign-OS
python -m http.server 8888 --directory examples
```

```bash
# Linux/macOS
cd Sovereign-OS
python3 -m http.server 8888 --directory examples
```

Leave this running. It serves e.g. `http://localhost:8888/ingest_demo_orders.json`.

### 2. Start Sovereign-OS with ingest and auto-approve

In a **second** terminal, set env and start the Web UI:

```powershell
# Windows
$env:SOVEREIGN_INGEST_URL = "http://localhost:8888/ingest_demo_orders.json"
$env:SOVEREIGN_INGEST_INTERVAL_SEC = "15"
$env:SOVEREIGN_AUTO_APPROVE_JOBS = "true"
$env:SOVEREIGN_COMPLIANCE_AUTO_PROCEED = "true"
$env:SOVEREIGN_JOB_DB = ".\data\jobs.db"
$env:SOVEREIGN_LEDGER_PATH = ".\data\ledger.jsonl"
python -m sovereign_os.web.app
```

```bash
# Linux/macOS
export SOVEREIGN_INGEST_URL=http://localhost:8888/ingest_demo_orders.json
export SOVEREIGN_INGEST_INTERVAL_SEC=15
export SOVEREIGN_AUTO_APPROVE_JOBS=true
export SOVEREIGN_COMPLIANCE_AUTO_PROCEED=true
export SOVEREIGN_JOB_DB=./data/jobs.db
export SOVEREIGN_LEDGER_PATH=./data/ledger.jsonl
python -m sovereign_os.web.app
```

### 3. Watch the flow

- Open **http://localhost:8000**.
- Within about 15 seconds the ingest poller fetches the JSON and enqueues the job(s). They appear in **Job queue** and, with auto-approve, move to approved and then **running**.
- In **Decision stream** you see: CEO plan → CFO approval → dispatch (permission OK) → tasks → audit → completion.
- **Trust** shows the agent’s score (default 50; +5 / -15 after audit).
- When the job completes: Stripe charges (test), Ledger gets `job_income`, and if `SOVEREIGN_WEBHOOK_URL` is set, your endpoint gets the delivery POST.

To see **permission denied** (firewall): use a Charter or worker that requests a high-threshold capability (e.g. SPEND_USD 80) while the agent’s score is still low, or temporarily lower the base TrustScore in code for demo.

---

## Option B: Simulate orders with a script (no ingest server)

If you prefer not to run the HTTP server, you can push jobs from the command line to simulate “orders from the web”.

### 1. Start the Web UI

```powershell
# Windows
$env:SOVEREIGN_AUTO_APPROVE_JOBS = "true"
$env:SOVEREIGN_JOB_DB = ".\data\jobs.db"
$env:SOVEREIGN_LEDGER_PATH = ".\data\ledger.jsonl"
python -m sovereign_os.web.app
```

### 2. In another terminal, POST a job (simulated order)

```powershell
# Windows
.\examples\demo_paid_job.ps1
# or
Invoke-RestMethod -Uri "http://localhost:8000/api/jobs" -Method POST -ContentType "application/json" -Body '{"goal":"Summarize the benefits of remote work in one paragraph.","amount_cents":500,"currency":"USD"}'
```

```bash
# Linux/macOS
curl -X POST http://localhost:8000/api/jobs \
  -H "Content-Type: application/json" \
  -d '{"goal":"Summarize the benefits of remote work in one paragraph.","amount_cents":500,"currency":"USD"}'
```

Then watch the same flow in the Dashboard: CEO → CFO → permissions → run → delivery → Stripe.

---

## Delivery (webhook) demo

- Set `SOVEREIGN_WEBHOOK_URL` to a URL that receives POSTs (e.g. [webhook.site](https://webhook.site), or a small Flask/FastAPI server that prints the body).
- When a job completes or goes `payment_failed`, Sovereign-OS POSTs a JSON payload (e.g. `job_id`, `status`, `goal`, `amount_cents`, `result_summary`, `completed_at`). That is your “delivery” event; in production your backend would notify the customer (email, Slack, etc.).

---

## One-command demo script (Option A in one go)

Use the script that starts the file server and the app with the right env (see below). It prints what to open and what each phase demonstrates.

- **examples/demo_full_flow.ps1** (Windows): starts HTTP server in background, sets env, runs the app (or instructs you to run it in a second terminal).
- **examples/demo_full_flow.sh** (Linux/macOS): same idea.

Run from project root:

```powershell
.\examples\demo_full_flow.ps1
```

```bash
./examples/demo_full_flow.sh
```

Then open http://localhost:8000 and watch Job queue, Decision stream, Trust, Balance, and (if configured) your webhook endpoint.

---

## Checklist so the demo is “real”

| Item | Check |
|------|--------|
| Ingest | `SOVEREIGN_INGEST_URL` points to a URL that returns JSON (array or `{"jobs": [...]}`) with `goal`, optional `amount_cents`, `currency`. |
| CEO/CFO | Dashboard Decision stream shows “CEO: Plan created” and “CFO: Approved” (or denied). |
| Permissions | Trust bar and stream show scores and “permission OK”; agents need sufficient TrustScore for the capability (see `sovereign_os/agents/auth.py`: READ_FILES 10, CALL_EXTERNAL_API 50, SPEND_USD 80, etc.). |
| Delivery | `SOVEREIGN_WEBHOOK_URL` or per-job `callback_url` receives POST on completion. |
| Stripe | `STRIPE_API_KEY` set; `amount_cents > 0`; after run, Ledger has `job_income` and Stripe test Dashboard shows the payment. |

This gives you one coherent story: **auto orders from the web → CEO/CFO → agent firewall (TrustScore) → delivery → earn via Stripe**, all with real logic and no fake steps.
