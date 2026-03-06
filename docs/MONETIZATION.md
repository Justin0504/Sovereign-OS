# Monetization: job queue, Stripe, and human approval

Sovereign-OS can **make money** by ingesting external jobs, running missions, and charging via Stripe. High-value or high-risk work stays behind a **human approval** gate.

## End-to-end flow

1. **Jobs enter the system**  
   - **Option A:** Something (cron, MCP, your backend) `POST`s to `POST /api/jobs` with `goal`, `charter`, optional `amount_cents`, `currency`.  
   - **Option B:** Set `SOVEREIGN_INGEST_URL` to a JSON endpoint; the ingest worker polls it and enqueues jobs (see [CONFIG.md](CONFIG.md)).

2. **Jobs sit in `pending`**  
   - They appear in the Web Dashboard under **Job queue**.  
   - No mission runs until a human (or your automation) approves.

3. **Human approval**  
   - In the Dashboard, click **Approve** on a pending job.  
   - Status becomes `approved`. A background worker picks it up and runs the mission (Charter → plan → CFO → dispatch → audit).

4. **Mission runs**  
   - If you wired a **compliance hook** (e.g. spend above a threshold), the engine may raise **Human approval required** and put the job back to `pending` with a message; a second approval (or override) is then required.  
   - Otherwise the mission runs to completion and tasks are audited.

5. **Charge and record income**  
   - When the mission completes and audit passes, the app calls the **payment service** (e.g. Stripe).  
   - On success: `payment_id` is stored on the job, and **UnifiedLedger** gets `record_usd(amount_cents, purpose="job_income", ref="job-{id}")`.  
   - On failure: job status becomes `payment_failed` and the error is stored; Ledger is not updated.

6. **You see P&L**  
   - Ledger shows income (job_income) and expenses (task burn). Use `ledger.total_usd_cents()`, `ledger.entries()`, or the Dashboard for balance and token usage.

7. **Delivery / proactive contact (optional)**  
   - When a job becomes `completed` or `payment_failed`, you can have the system **POST** a JSON payload to a URL: set `SOVEREIGN_WEBHOOK_URL` or pass `callback_url` per job in `POST /api/jobs`.  
   - Your backend receives the payload (job_id, status, goal, result_summary, payment_id, etc.) and can then **notify the customer** (email, Slack, SMS) or update your CRM.  
   - Optional `SOVEREIGN_WEBHOOK_SECRET` adds an HMAC signature so the receiver can verify the request. See [CONFIG.md](CONFIG.md).

## Configuration

| What | How |
|------|-----|
| **Stripe** | Set `STRIPE_API_KEY`. Optionally `STRIPE_WEBHOOK_SECRET` and point Stripe to `POST /api/webhooks/stripe`. |
| **Job DB** | Set `SOVEREIGN_JOB_DB` (path to SQLite) so jobs persist across restarts. |
| **Ingest URL** | Set `SOVEREIGN_INGEST_URL` to poll for new work; format is in [CONFIG.md](CONFIG.md). |
| **API key for POST /api/jobs** | Set `SOVEREIGN_API_KEY`; callers send `X-API-Key` or `Authorization: Bearer <key>`. |
| **Compliance (spend threshold)** | Pass a `compliance_hook` and `spend_threshold_cents` into `GovernanceEngine` / Treasury; when a task’s estimated cost exceeds the threshold, the hook can return `RequestHumanApproval` so the job is put back to pending. |

## Human approval in practice

- **First gate:** Jobs start as `pending`. Someone (or your policy) approves in the Dashboard → status `approved` → mission runs.  
- **Second gate (optional):** If the compliance hook is configured and a task’s budget is above the threshold, the engine raises and the job can be set back to `pending` with a message like “Human approval required for spend above $X.” A second approval (or a “force run” / override in your UI) then allows the mission to proceed.  
- **Stripe:** Charging happens only after the mission and audit succeed; idempotency by `job_id` avoids double charges.

## Human-out-of-loop (fully autonomous)

To run **without** human approval:

- Set **`SOVEREIGN_AUTO_APPROVE_JOBS=true`**: every new job (from API or ingest) is immediately `approved` and picked up by the worker. No Dashboard click required.
- Optionally set **`SOVEREIGN_COMPLIANCE_AUTO_PROCEED=true`**: when a task’s cost is above the compliance threshold, the CFO still allows it (no second approval). Use only if you accept the risk.

With both set, the flow is: **ingest or API → auto-approve → run → audit → charge → webhook**. See [CONFIG.md](CONFIG.md).

## Summary

| Step | Who / What |
|------|------------|
| Jobs created | `POST /api/jobs` or ingest worker |
| Approval to run | Human (Dashboard Approve) or automation |
| Approval for high spend (optional) | Compliance hook → job back to pending until second approval |
| Charge | Stripe (or other payment service) after mission + audit |
| Income on ledger | `record_usd(..., purpose="job_income")` |
| Notify customer | Configure `SOVEREIGN_WEBHOOK_URL` or per-job `callback_url`; your backend receives the completion payload and sends email/Slack/etc. |

See [CONFIG.md](CONFIG.md) for all env vars and [PHASE6.md](PHASE6.md) for the compliance hook interface.
