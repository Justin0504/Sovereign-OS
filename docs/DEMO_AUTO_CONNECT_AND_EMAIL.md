# Demo: Auto-Connect & Email Delivery

Make your paid demo **automatically receive orders** and **deliver results by email** so it can run with minimal manual steps and feel like a real service.

---

## 1. Where You Are Now

- **Orders**: Created via `POST /api/jobs` (curl, script, or ingest URL). Optional `callback_url` → you POST the result to that URL when the job completes.
- **Payment**: Stripe charges on completion.
- **Gap**: Most customers expect **email**: “I sent my request, charge me, then **email me the result**.” Right now you only have webhook (callback_url), no outbound email.

---

## 2. Optimization Goals

| Goal | What it means |
|------|----------------|
| **Auto-connect** | Orders arrive without you pasting curl: form, email inbox, or external webhook → create jobs. |
| **Send email** | When a job completes, optionally send the result to the customer’s email (in addition to or instead of callback_url). |

---

## 3. Auto-Connect: How Orders Get In

### Option A: Public order form (simplest)

- **Idea**: A small webpage or Typeform/Google Form where the customer enters:
  - Email
  - Request (goal)
  - (Optional) Amount or product choice
- **Back-end**: A serverless function (Vercel/Netlify) or a tiny Flask/FastAPI app that:
  - Receives the form POST
  - Validates and maps to `goal`, `amount_cents`, and (see below) `customer_email`
  - Calls your `POST /api/jobs` with `X-API-Key` (or runs on a server that has `SOVEREIGN_API_KEY` in env)
- **Result**: Customer submits form → job created → your demo runs → you deliver (webhook + email).

### Option B: Email intake (inbox → jobs)

- **Idea**: A script or cron job that:
  - Reads an inbox (e.g. `orders@yourdomain.com`) via IMAP
  - Parses “order” emails (subject/body or a simple format like “Goal: … Amount: 5 USD”)
  - For each new order, calls `POST /api/jobs` with `goal`, `amount_cents`, and `customer_email` = sender
- **Result**: Customer sends email → script creates job → demo runs → you send result back by email.

### Option C: External platform webhook

- **Idea**: If you list on a marketplace or use a tool (e.g. Zapier, Make, n8n) that can send HTTP on “new order”:
  - Configure the webhook to hit your API (or a small adapter) that builds `goal` / `amount_cents` / `customer_email` and calls `POST /api/jobs`.
- **Result**: Order on platform → webhook → job created → demo runs → email (and/or callback) for delivery.

**Recommendation**: Start with **Option A (order form)** for a clean demo; add **Option B** if you want “reply to this email to order.”

---

## 4. Send Email: Deliver Results to the Customer

### 4.1 Add `customer_email` to the job

- In `POST /api/jobs` (and batch), accept an optional field: `customer_email`.
- Store it on the job (e.g. extend `Job` and the store). No need to validate beyond “looks like an email” if you prefer to keep it simple.

### 4.2 When the job completes, send one email

- **Trigger**: In the same place you call `_fire_job_webhook` (after payment success), if `job.customer_email` is set, call a small **email sender**.
- **Content**: One email per completed job, e.g.:
  - **Subject**: `Your order #<job_id> is ready`
  - **Body**: Include `goal`, a short result summary (e.g. first 500 chars of the task output or a link), and “You were charged $X.XX.”
- **Sender**: Use either:
  - **SMTP** (Gmail app password, or any SMTP): env vars like `SOVEREIGN_SMTP_HOST`, `SOVEREIGN_SMTP_PORT`, `SOVEREIGN_SMTP_USER`, `SOVEREIGN_SMTP_PASSWORD`, `SOVEREIGN_FROM_EMAIL`.
  - **Transactional API** (e.g. SendGrid, Mailgun): `SENDGRID_API_KEY` + one API call to send the same content.

### 4.3 Keep callback_url as-is

- If the customer also provided `callback_url`, keep calling `notify_job_completion` as you do today. Email is an **additional** channel so you can support “email-only” customers (no webhook).

---

## 5. Implementation Checklist

### Phase 1: Email delivery (so “real money” demo delivers by email)

1. **Extend job model and API**
   - Add `customer_email: str | None` to `Job` and to the job store (SQLite + Redis if you use them).
   - In `POST /api/jobs` and batch, read `customer_email` from the body and persist it.

2. **Add email sender module**
   - New module, e.g. `sovereign_os/delivery/email.py` (or under `web/`):
     - `send_job_result_email(to_email: str, job_id: int, goal: str, result_summary: str, amount_charged: str) -> None`
     - Use either SMTP (e.g. `smtplib`) or SendGrid REST API, driven by env (e.g. `SENDGRID_API_KEY` or `SOVEREIGN_SMTP_*`).
   - If no email config, no-op (log “email not configured, skip”).

3. **Call sender on job completion**
   - Where you call `_fire_job_webhook(job, "completed", ...)` after successful payment, add:
     - If `job.customer_email`: call `send_job_result_email(job.customer_email, job.job_id, job.goal, result_summary, "$X.XX")`.
   - Build `result_summary` from the mission result (e.g. concatenate task outputs or first N characters).

4. **Config and docs**
   - Document in `CONFIG.md`: `SOVEREIGN_SMTP_*` or `SENDGRID_API_KEY`, `SOVEREIGN_FROM_EMAIL`.
   - In `PAID_DEMO.md` (or this doc): “To get the result by email, pass `customer_email` in the job body.”

### Phase 2: Auto-connect (order form or email intake)

5. **Order form**
   - Add a minimal HTML form (or link to a hosted form) that posts to:
     - Your own endpoint that forwards to `POST /api/jobs` with API key, or
     - A serverless function that calls your API.
   - Form fields: email (→ `customer_email`), request (→ `goal`), optional amount (→ `amount_cents`).

6. **Optional: email intake**
   - Small script (or background job): IMAP poll → parse “order” emails → `POST /api/jobs` with `goal`, `amount_cents`, `customer_email` = sender. Run via cron or a long-running worker.

---

## 6. Suggested order of work

1. **Implement Phase 1** (customer_email + send one email on completion). That gives you “real money + email delivery” with your existing curl/Postman or form that posts to the API.
2. **Add a simple order form** (Phase 2, step 5) so customers (or you testing) can submit without curl.
3. **Optionally** add email intake (Phase 2, step 6) for “reply to this email to order.”

This keeps the demo automatically connected (form or email → job) and delivering by email, with minimal new surface area (one new field, one new module, one call site).
