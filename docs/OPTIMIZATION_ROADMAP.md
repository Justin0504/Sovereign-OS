# Optimization Roadmap

Building on the existing "ingest → auto-approve → run → audit → charge → webhook" loop and 24/7 design, the following directions are ordered by **priority** and **effort**: quick wins, medium-term, long-term.

**Done (short-term order):** Webhook failure logging, POST /api/jobs rate limit, Dashboard mode hints, Job input validation, failed job retry (`POST /api/jobs/{id}/retry` + `SOVEREIGN_JOB_MAX_RETRIES`), E2E with webhook mock. See [CONFIG.md](CONFIG.md) for new env vars and /health fields.

---

## 1. Reliability & ops

| Direction | Description | Priority |
|-----------|--------------|----------|
| **Failed job retry** ✅ | Support retry for `failed` / `payment_failed` jobs (configurable count, retriable errors only). `POST /api/jobs/{id}/retry`, `SOVEREIGN_JOB_MAX_RETRIES`. | High |
| **Graceful shutdown** | On SIGTERM, wait for the current job to finish before exiting. | Medium |
| **Job concurrency** | Add config (e.g. `SOVEREIGN_JOB_WORKER_CONCURRENCY=2`) to run multiple jobs in parallel. | Medium |
| **Queue & Ledger backup** | Docs or scripts for periodic backup of `SOVEREIGN_JOB_DB` and `SOVEREIGN_LEDGER_PATH`. | Low |

---

## 2. Observability & debugging

| Direction | Description | Priority |
|-----------|--------------|----------|
| **Webhook failure logging** ✅ | On webhook POST failure, write payload and error to `data/webhook_log.jsonl` (or configurable path). `SOVEREIGN_WEBHOOK_LOG_PATH`. | High |
| **POST /api/jobs rate limit** ✅ | Limit requests per minute by IP or API key (e.g. `SOVEREIGN_JOB_RATE_LIMIT_PER_MIN=60`). | High |
| **/health or /metrics** | Expose more: queue `pending`/`running` counts, last job completion time, optional Prometheus job throughput/latency. | Medium |
| **Request/Job trace ID** | Generate `request_id` per job or request; include in logs and webhook payload for traceability. | Medium |

---

## 3. Security & compliance

| Direction | Description | Priority |
|-----------|--------------|----------|
| **Job input validation** ✅ | Validate `goal` length, `amount_cents` bounds, `callback_url` format on `POST /api/jobs`. | High |
| **Production checks** | At startup, if production config is detected (e.g. `sk_live_`, no `SOVEREIGN_API_KEY`), warn in health or logs. | Medium |
| **IP allowlist (optional)** | Optional IP allowlist for `POST /api/jobs` or ingest source. | Low |

---

## 4. UX & onboarding

| Direction | Description | Priority |
|-----------|--------------|----------|
| **Dashboard mode hints** ✅ | Show in UI whether "auto-approve" and "compliance auto-proceed" are on. `/health` returns `auto_approve_jobs` / `compliance_auto_proceed`. | High |
| **One-click deploy example** | Provide docker-compose or single-node script with `.env.example` and recommended volumes. | Medium |
| **Example ingest endpoint** | Static JSON or minimal mock for testing `SOVEREIGN_INGEST_URL`. | Medium |
| **README screenshot/GIF** | Replace placeholder with real Dashboard screenshot or short video. | Medium |

---

## 5. Scale & performance

| Direction | Description | Priority |
|-----------|--------------|----------|
| **Multi-instance queue** | For horizontal scaling, add Redis (or similar) as queue backend and shared job state. | Low |
| **Charter/config cache** | Cache Charter and config after load to avoid repeated file reads. | Low |

---

## 6. Features

| Direction | Description | Priority |
|-----------|--------------|----------|
| **assistant_chat worker** | General Q&A; when goal has no clear "write/translate/minutes" etc., Strategist can use `assistant_chat`. | Medium |
| **code_assistant / code_review** | Code understanding, edit suggestions, simple code review (LLM only, no execution). | Medium |
| **Batch job API** | `POST /api/jobs/batch` to submit multiple goals in one call. | Low |
| **Job priority or schedule** | Queue supports priority or "run after" time. | Low |

---

## 7. Testing & quality

| Direction | Description | Priority |
|-----------|--------------|----------|
| **E2E with webhook mock** ✅ | E2E: create job → auto-approve → complete → assert webhook called with expected payload. | High |
| **Rate limit / boundary unit tests** | Unit tests for rate limit, `amount_cents` bounds, error response codes. | Medium |
| **Recovery tests** | After killing the process, restart and verify queue and Ledger restore correctly. | Low |

---

## 8. Community & outreach

| Direction | Description | Priority |
|-----------|--------------|----------|
| **Good First Issue labels** | Use `good first issue` (or similar) for docs, examples, unit-test issues. | Medium |
| **Changelog & releases** | Maintain CHANGELOG by version; tag releases. | Medium |

---

## Suggested order (short-term)

1. ~~Webhook failure logging + POST /api/jobs rate limit~~ ✅ Done.
2. ~~Dashboard mode hints + Job input validation~~ ✅ Done.
3. ~~Failed job retry + E2E with webhook mock~~ ✅ Done.

Next: graceful shutdown, /health enhancements (pending/running counts), rate-limit/boundary unit tests, production checks. Many of these are already done in Wave 1–4 (graceful shutdown, /health, request_id, Prometheus job metrics, Redis queue, job priority/run_after, validation tests, constant-time API key compare, etc.).

---

## Next optimizations (by priority)

### High value, quick

| Direction | Description | Output |
|-----------|--------------|--------|
| **README screenshot** | One Dashboard screenshot as `docs/dashboard.png` and reference in README. | 1 image |
| **test_web_api in CI** ✅ | Pin `httpx>=0.24,<0.28` in CI so TestClient works; CI stays green. | Done |
| **Dashboard priority / run_after_ts** ✅ | Job list shows P{priority} and scheduled run time. | Done |

### UX & observability

| Direction | Description | Output |
|-----------|--------------|--------|
| **Job list pagination** ✅ | `GET /api/jobs?limit=N` (default 100, max 500), returns `jobs` and `total`. | Done |
| **callback_url SSRF protection** ✅ | `validate_job_input` rejects localhost and private/loopback IPs; unit test. | Done |
| **Prometheus in TUI mode** | If starting via CLI/TUI (not Web), optional Prometheus HTTP on e.g. 9464. | Optional |

### Features & scale

| Direction | Description | Output |
|-----------|--------------|--------|
| **Charter hot reload or cache** | Currently read once at startup; optional hot reload or `POST /admin/reload_charter` (auth required). | Low |
| **More E2E** | E2E or integration tests for Redis queue, `POST /api/jobs/batch`, priority/run_after. | 3–5 cases |

### Community

| Direction | Description | Output |
|-----------|--------------|--------|
| **Good-first-issue** | Point contributors to [GOOD_FIRST_ISSUES.md](GOOD_FIRST_ISSUES.md); tag issues. | Labels + doc |
| **Release & tag** | Cut v0.4.0 (or current) per [RELEASE.md](RELEASE.md); GitHub Release with CHANGELOG link. | 1 release |

**Suggested order:** ① Add `docs/dashboard.png` and wire in README; ~~② Pin httpx and fix test_web_api in CI~~ ✅; ~~③ Dashboard priority/run_after~~ ✅; ~~④ callback_url SSRF and job list pagination~~ ✅.
