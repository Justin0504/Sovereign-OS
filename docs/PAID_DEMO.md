# Paid Jobs Demo: Step-by-Step

Goal: Use Sovereign-OS to **take real jobs, run tasks, charge via Stripe**, and optionally send results back to the client. Follow the steps below to run a full **order → execution → charge → delivery** loop.

---

## How the Demo Shows CEO / CFO / Dynamic Permissions

When you run a paid job, the system already does these three things; the Dashboard **Decision stream** and **Trust** reflect them.

| Role | What it does | Where you see it |
|------|--------------|------------------|
| **CEO (Strategist)** | Splits the client goal into a task plan: N tasks, each with a skill (e.g. summarize / research) and estimated tokens. | Decision stream: **"CEO: Plan created — N tasks. Goal: …"**; Tasks card shows each task's skill. |
| **CFO (Treasury)** | **Budget approval** before each task: balance check, daily burn cap; high amounts can trigger a second compliance check. | Decision stream: **"CFO: Approved N task(s), est. $X. Balance: $Y."**; if balance is insufficient the job fails and logs **CFO denied budget**. |
| **Dynamic permissions (SovereignAuth)** | Before each task dispatch, checks the **agent TrustScore** against capability thresholds (e.g. SPEND_USD requires 80). Audit pass → score up; fail → score down, next time may be denied. | Top bar **Trust** shows the current agent's TrustScore; **"CFO dispatch: Task X → agent (permission OK)"** means permission passed. Repeated audit failures can lead to **permission_denied**. |

**How to see "dynamic permissions" more clearly:**  
- **Trust** in the top bar is the current agent's TrustScore (default 50; +5 on audit pass, -15 on fail).  
- Capability thresholds: READ_FILES 10, SPEND_USD 80, etc. (see `sovereign_os/agents/auth.py`).  
- To see "permission denied": lower an agent's TrustScore (or trigger audit failure with empty/bad output), then run another job.

---

## 1. Setup (~10 minutes)

### 1.1 Stripe account

- Sign up or log in at [Stripe](https://stripe.com).
- **Test mode**: Get a Test mode key `sk_test_...` under [Developers → API keys](https://dashboard.stripe.com/test/apikeys).
- **Live**: Switch to Live mode and use `sk_live_...` (confirm environment and compliance before going live).

### 1.2 LLM API key

- Use either **OpenAI** (`OPENAI_API_KEY`) or **Anthropic** (`ANTHROPIC_API_KEY`).
- Used for CEO planning, auditing, and built-in workers (summarize, research, write, etc.).

### 1.3 Install and configure Sovereign-OS

```bash
cd Sovereign-OS
pip install -e ".[llm,payments]"
cp .env.example .env
```

Edit `.env` and set at least:

```env
STRIPE_API_KEY=sk_test_xxxx
ANTHROPIC_API_KEY=sk-ant-xxxx
# or OPENAI_API_KEY=sk-xxxx
```

Optional but recommended (persistence and auto-approve):

```env
SOVEREIGN_JOB_DB=./data/jobs.db
SOVEREIGN_LEDGER_PATH=./data/ledger.jsonl
SOVEREIGN_AUTO_APPROVE_JOBS=true
SOVEREIGN_COMPLIANCE_AUTO_PROCEED=true
```

- `SOVEREIGN_AUTO_APPROVE_JOBS=true`: New jobs are approved automatically (no Approve click in Dashboard); good for a "real orders" demo.
- `SOVEREIGN_COMPLIANCE_AUTO_PROCEED=true`: If you set a spend threshold, exceeding it still auto-proceeds without a second manual step.

---

## 2. Pricing and job content

- Each job’s amount is **`amount_cents`** (cents), e.g.:
  - Summary: 500 cents = $5  
  - Research/writing: 1000 cents = $10  
- When creating a job, send `goal` and `amount_cents` in the body, e.g.:
  - `"goal": "Summarize the key points of quantum computing in 3 paragraphs."`
  - `"amount_cents": 500`

---

## 3. Where orders come from (pick one)

### Option A: curl / Postman to simulate a client order (fastest)

With the server running, create a paid job via the API:

```bash
# Start server (in one terminal)
python -m sovereign_os.web.app
```

In another terminal:

```bash
curl -X POST http://localhost:8000/api/jobs \
  -H "Content-Type: application/json" \
  -d '{
    "goal": "Summarize the benefits of remote work in one paragraph.",
    "amount_cents": 500,
    "currency": "USD"
  }'
```

If `SOVEREIGN_API_KEY` is set, add one of:

- `X-API-Key: your-key`
- or `Authorization: Bearer your-key`

Response includes `job_id`, `status`, etc. The job appears in the Dashboard (http://localhost:8000) Job queue; with auto-approve it goes to approved and runs.

### Option B: Ingest polling (for JSON from a script or backend)

1. Expose an HTTP-accessible JSON URL returning an array like:

```json
[
  { "goal": "Summarize X in one paragraph.", "amount_cents": 500, "currency": "USD" },
  { "goal": "Research pros and cons of Y.", "amount_cents": 1000, "currency": "USD" }
]
```

Or `{ "jobs": [ ... ] }` (see [CONFIG.md](CONFIG.md)).

2. In `.env`:

```env
SOVEREIGN_INGEST_URL=https://your-domain-or-internal/orders.json
SOVEREIGN_INGEST_INTERVAL_SEC=60
SOVEREIGN_INGEST_DEDUP_SEC=300
```

3. Restart the web app. The poller will enqueue new jobs; with `SOVEREIGN_AUTO_APPROVE_JOBS=true` you get auto accept, run, and charge.

### Option C: Simple order form POSTing to your API

1. Build a static page or small backend with a form: task description (`goal`), amount (as `amount_cents`), optional `callback_url` for delivery.
2. On submit, call:

   `POST https://your-domain/api/jobs`  
   Body: `{ "goal": "...", "amount_cents": 500, "currency": "USD", "callback_url": "optional" }`

3. If `SOVEREIGN_API_KEY` is set, include the key in your backend (do not hardcode in frontend; prefer a backend proxy). You can also use `SOVEREIGN_JOB_IP_WHITELIST` to allow only your server IP.

---

## 4. Delivery / notify client (webhook)

When a job completes or payment fails, Sovereign-OS POSTs to a URL so you can notify the client or update your DB.

1. Use a **public URL** (your backend), e.g.  
   `https://your-domain/webhook/sovereign-job-done`

2. In `.env`:

```env
SOVEREIGN_WEBHOOK_URL=https://your-domain/webhook/sovereign-job-done
SOVEREIGN_WEBHOOK_SECRET=your-secret
```

3. In your backend:
   - Verify the request with `SOVEREIGN_WEBHOOK_SECRET` and the `X-Sovereign-Signature` header (HMAC-SHA256).
   - Parse the JSON: `job_id`, `status`, `goal`, `result_summary`, `payment_id`, etc.; use it to email/Slack the client or update order status.

You can also pass `callback_url` per job; that URL is POSTed when that job completes (same payload shape). See [CONFIG.md](CONFIG.md).

---

## 5. Run one full flow: order → run → charge → deliver

1. **Start**  
   ```bash
   python -m sovereign_os.web.app
   ```
   Open http://localhost:8000 for the Dashboard.

2. **Send one job** (Option A curl, or your Option B/C entry point)  
   e.g. `goal` = "Summarize the benefits of open source in 3 bullet points.", `amount_cents` = 500.

3. **Check**  
   - Job queue: pending → approved → running → completed (if auto-approve is on).  
   - Balance / Ledger: you should see income (e.g. +$5.00).  
   - Stripe Dashboard (Test): [Payments](https://dashboard.stripe.com/test/payments) should show the charge.

4. **Delivery**  
   - If `SOVEREIGN_WEBHOOK_URL` is set, your backend gets the completion POST; use `result_summary` etc. to notify the client or update status.

---

## 6. Checklist (so it actually "makes money")

| Item | Notes |
|------|--------|
| Stripe key | `.env` has `sk_test_...` (test) or `sk_live_...` (live); restart after change. |
| LLM key | At least one of `OPENAI_API_KEY` or `ANTHROPIC_API_KEY`; otherwise tasks may use stubs with no real output. |
| Auto-approve | With `SOVEREIGN_AUTO_APPROVE_JOBS=true`, new jobs run automatically; otherwise click Approve in the Dashboard. |
| Amount | `amount_cents > 0` for Stripe charge; 0 means run task only, no charge. |
| Webhook | If you get no callback, check `SOVEREIGN_WEBHOOK_LOG_PATH` (failures are logged), network, and that the URL is reachable from the machine running Sovereign. |

---

## 7. Minimal runnable flow

1. Set `.env`: `STRIPE_API_KEY`, one LLM key, `SOVEREIGN_AUTO_APPROVE_JOBS=true` (and optional Ledger/Job DB paths).  
2. Start: `python -m sovereign_os.web.app`.  
3. Send one job via curl or your form: `POST /api/jobs` with `goal` and `amount_cents`.  
4. Confirm in Dashboard and Stripe: job completed, charge present, Ledger shows income.  
5. For "notify client": set `SOVEREIGN_WEBHOOK_URL` (and optional `SOVEREIGN_WEBHOOK_SECRET`); in your backend handle the POST and send email/Slack etc.

For more env vars and security, see [CONFIG.md](CONFIG.md); for billing and compliance, see [MONETIZATION.md](MONETIZATION.md).

**Full demo narrative (auto ingest, CEO/CFO, permissions, delivery, Stripe):** [DEMO_SCRIPT.md](DEMO_SCRIPT.md).
