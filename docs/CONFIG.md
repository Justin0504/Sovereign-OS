# Configuration & environment variables

All optional unless noted. Set in the shell, `.env`, or your process manager (e.g. Docker Compose).

## Core

| Variable | Description |
|----------|-------------|
| `SOVEREIGN_LEDGER_PATH` | Path to the ledger JSONL file (append-only USD/token log). If unset, ledger is in-memory. |
| `SOVEREIGN_JOB_DB` | Path to the SQLite job queue DB (e.g. `jobs.db`). Used only when `REDIS_URL` is not set. If both unset, jobs are in-memory. |
| `REDIS_URL` | When set, use Redis as job store and shared approved queue (multi-instance workers). Install with `pip install -e ".[deploy]"`. |
| `SOVEREIGN_CHARTER` | Default charter name shown in the Web UI (e.g. `Default`). |

## Web UI & API

| Variable | Description |
|----------|-------------|
| `SOVEREIGN_API_KEY` | If set, `POST /api/jobs` requires `X-API-Key` or `Authorization: Bearer <key>`. |
| `SOVEREIGN_JOB_RATE_LIMIT_PER_MIN` | If set (e.g. `60`), limits how many `POST /api/jobs` requests each client IP can make per minute; returns 429 when exceeded. |
| `SOVEREIGN_JOB_MAX_RETRIES` | Max retries for a single job via `POST /api/jobs/{id}/retry` (default `2`). Only `failed` / `payment_failed` jobs are retryable. |
| `SOVEREIGN_JOB_WORKER_CONCURRENCY` | Max jobs run in parallel by the worker (default `1`). Set to `2` or more for higher throughput. |
| `SOVEREIGN_JOB_IP_WHITELIST` | Optional. Comma-separated IPs allowed to call `POST /api/jobs` and `POST /api/jobs/batch`; others get 403. |
| (none) | Web UI: `GET /` (dashboard), `GET /health`, `GET /docs` (FastAPI Swagger). Dashboard shows “Auto-approve ON” / “Compliance auto ON” when those env vars are set. |

**`GET /health` response** (for operators): `status`, `ledger`, `redis`, `config_warnings`, `jobs_total`, `jobs_pending`, `jobs_running`, `last_job_completed_at` (Unix timestamp of last completed/failed job), `auto_approve_jobs`, `compliance_auto_proceed`. **Production hints in config_warnings:** when `STRIPE_API_KEY` contains `sk_live_` or when Stripe is set but `SOVEREIGN_API_KEY` is not, a warning is added. Use `config_warnings` to detect incomplete or unsafe production setup.

**Job creation:** `POST /api/jobs` validates `goal` length (max 20 000 chars), `amount_cents` (0–1 000 000), and `callback_url` (must be valid http(s) URL). Rate limit: set `SOVEREIGN_JOB_RATE_LIMIT_PER_MIN` (per client IP). Optional body: `priority` (int; higher runs first), `run_after_ts` or `run_after_sec`. Failed/payment_failed jobs can be retried via `POST /api/jobs/{id}/retry` up to `SOVEREIGN_JOB_MAX_RETRIES` (default 2). **Metrics:** `GET /metrics` returns Prometheus text (sovereign_jobs_completed_total, sovereign_job_duration_seconds, sovereign_jobs_pending/running).

## 24/7 & ingestion

The Web process is designed for **24/7** operation: a background thread continuously picks approved jobs from the queue (every few seconds), and when `SOVEREIGN_INGEST_URL` is set, another thread polls that URL for new work. Run the process under Docker (`restart: unless-stopped`), systemd, or another process manager so it stays up and restarts on failure. Use `SOVEREIGN_JOB_DB` and `SOVEREIGN_LEDGER_PATH` so jobs and ledger persist across restarts.

| Variable | Description |
|----------|-------------|
| `SOVEREIGN_INGEST_URL` | JSON URL to poll for new jobs. Response: array or `{ "jobs": [...] }` with `goal`, optional `charter`, `amount_cents`, `currency`. |
| `SOVEREIGN_INGEST_INTERVAL_SEC` | Polling interval in seconds (default `60`). |
| `SOVEREIGN_INGEST_DEDUP_SEC` | If set (e.g. `300`), the ingest poller will not enqueue a job when one with the same `goal` and `amount_cents` was already created within this many seconds. Reduces duplicate jobs from repeated polling. |

## Audit trail (verifiable)

| Variable | Description |
|----------|-------------|
| `SOVEREIGN_AUDIT_TRAIL_PATH` | Path to JSONL file where each `AuditReport` is appended (with `proof_hash`). Enables `GET /api/audit_trail`. |

## Compliance (Phase 6b)

| Variable | Description |
|----------|-------------|
| `SOVEREIGN_COMPLIANCE_SPEND_THRESHOLD_CENTS` | If set to a positive number, the Web UI engine uses `ThresholdComplianceHook`: when a task’s estimated cost (cents) is ≥ this value, the job is put back to `pending` with “Human approval required” until a second approval. See [MONETIZATION.md](MONETIZATION.md) and [PHASE6.md](PHASE6.md). |
| `SOVEREIGN_COMPLIANCE_AUTO_PROCEED` | When set to `true` (or `1`/`yes`), the CFO does **not** require human approval when the compliance hook returns “request human approval” for spend above the threshold; the task is allowed to proceed (human-out-of-loop for high spend). Use only when you accept the risk. |

## Human out of loop

| Variable | Description |
|----------|-------------|
| `SOVEREIGN_AUTO_APPROVE_JOBS` | When set to `true` (or `1`/`yes`), every new job (from `POST /api/jobs` or ingest) is immediately set to `approved` so the worker runs it without a human clicking Approve. Full **human-out-of-loop**: ingest → auto-approve → run → charge → webhook. |
| `SOVEREIGN_COMPLIANCE_AUTO_PROCEED` | See Compliance above. Together with `SOVEREIGN_AUTO_APPROVE_JOBS`, both gates can be automated. |

## Job completion webhook (delivery / proactive contact)

| Variable | Description |
|----------|-------------|
| `SOVEREIGN_WEBHOOK_URL` | When a job reaches `completed` or `payment_failed`, a POST is sent to this URL with a JSON payload (job_id, status, goal, amount_cents, result_summary, audit_score, etc.). Optional. |
| `SOVEREIGN_WEBHOOK_SECRET` | If set, the request body is signed with HMAC-SHA256 and sent in header `X-Sovereign-Signature: sha256=<hex>`. Receivers should verify this. |
| `SOVEREIGN_WEBHOOK_LOG_PATH` | If set (default `data/webhook_log.jsonl` when logging is enabled), failed webhook deliveries (after all retries) are appended as one JSONL line per failure (url, job_id, status, error, ts). |
| Per-job `callback_url` | In `POST /api/jobs` body you can pass `callback_url`; when that job completes, the webhook is sent to this URL instead of (or in addition to) the global `SOVEREIGN_WEBHOOK_URL`. |
| (payload) | Each job gets a `request_id` (trace id) at creation; it is included in the webhook payload when set, for correlating logs and delivery. |

Payload format and retries: see [OPEN_SOURCE_READY_PLAN.md](OPEN_SOURCE_READY_PLAN.md) (Webhook 载荷规范). Typical use: your backend receives the webhook and then notifies the customer (email, Slack, etc.).

## Payments (Stripe)

| Variable | Description |
|----------|-------------|
| `STRIPE_API_KEY` | Stripe secret key for charges. |
| `STRIPE_WEBHOOK_SECRET` | Webhook signing secret; if set, `POST /api/webhooks/stripe` verifies `Stripe-Signature`. |

## Infrastructure

| Variable | Description |
|----------|-------------|
| `REDIS_URL` | Redis connection URL. If set, `GET /health` checks Redis; used for optional state/cache. |

## LLM & optional extras

- **LLM:** Configured via `sovereign_os.llm.providers` (e.g. `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`). See `[llm]` optional dependency. If only `ANTHROPIC_API_KEY` is set (no `OPENAI_API_KEY`), the default provider is `anthropic` with model `claude-3-5-sonnet-20241022`; no need to set `SOVEREIGN_LLM_PROVIDER` for Anthropic-only use.
- **Memory (ChromaDB):** Optional; see `[memory]` and `sovereign_os.memory.manager`.
- **Telemetry:** Optional OpenTelemetry/Prometheus; see `[telemetry]` and `sovereign_os.telemetry.tracer`.

## Example (Docker Compose)

```yaml
environment:
  SOVEREIGN_LEDGER_PATH: /app/data/ledger.jsonl
  SOVEREIGN_JOB_DB: /app/data/jobs.db
  SOVEREIGN_AUDIT_TRAIL_PATH: /app/data/audit.jsonl
  SOVEREIGN_API_KEY: "${SOVEREIGN_API_KEY}"
  REDIS_URL: redis://redis:6379/0
```

## CLI

- **Charter path:** Required for `sovereign run` (e.g. `-c charter.example.yaml`). Exits with error if the file is missing.
- **Ledger:** `sovereign run --ledger /path/to/ledger.jsonl` or `SOVEREIGN_LEDGER_PATH` to persist ledger.
- **Audit trail:** `sovereign run --audit-trail /path/to/audit.jsonl` or `SOVEREIGN_AUDIT_TRAIL_PATH` to append AuditReports.
- **Version:** `sovereign --version` prints the package version.
