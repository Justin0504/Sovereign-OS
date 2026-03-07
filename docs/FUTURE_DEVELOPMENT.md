# Future Development Plan

With the "ingest → approve → run → audit → charge → webhook" loop and 24/7 design in place, plus **16 built-in workers**, this doc outlines **phased, actionable** next steps for prioritization and issues.

**Wave 3 & 4 done:** assistant_chat / code_assistant / code_review workers, job concurrency (`SOVEREIGN_JOB_WORKER_CONCURRENCY`), `POST /api/jobs/batch`, multi-instance doc ([MULTI_INSTANCE.md](MULTI_INSTANCE.md)), Good First Issues doc, release process, recovery test (`test_job_store_persists_across_restart`), IP allowlist (`SOVEREIGN_JOB_IP_WHITELIST`).

---

## Current state (summary)

| Area | Done |
|------|------|
| **Core loop** | Ingest (API + Ingest), manual/auto approve, run & audit, Stripe charge, Ledger, completion webhook |
| **Open-source ready** | Default Charter, 16 built-in workers, QUICKSTART, config_warnings, input validation, rate limit, retry, webhook failure logging, batch API, job concurrency, IP allowlist |
| **Observability** | /health (queue & mode hints), token usage, audit trail, optional Prometheus |

See [OPEN_SOURCE_READY_PLAN.md](OPEN_SOURCE_READY_PLAN.md) and [OPTIMIZATION_ROADMAP.md](OPTIMIZATION_ROADMAP.md).

---

## Wave 1: Stability & observability (recommended first)

**Goal:** More stable production and easier debugging. Low effort, high value.

| # | Direction | Action | Ref |
|---|-----------|--------|-----|
| 1.1 | **Graceful shutdown** | On SIGTERM, wait for current job to finish before exit | OPTIMIZATION_ROADMAP §1 |
| 1.2 | **/health** | Expose `jobs_running`, `jobs_pending`; optional last job completion time or queue depth | §2 |
| 1.3 | **request_id / trace** | Generate `request_id` per job or API request; include in logs and webhook payload | §2 |
| 1.4 | **Production checks** | At startup, if `sk_live_` or no `SOVEREIGN_API_KEY`, warn in /health or logs | §3 |
| 1.5 | **Rate limit & boundary tests** | Unit tests for `POST /api/jobs` rate limit, `amount_cents` bounds, 400/429 responses | §7 |

**Done when:** Safe restarts in production; ops can infer queue and config from /health and logs; critical paths have unit tests.

---

## Wave 2: UX & deployment

**Goal:** Easier onboarding and 24/7 deployment.

| # | Direction | Action | Ref |
|---|-----------|--------|-----|
| 2.1 | **One-click deploy** | docker-compose or single-node script with `.env.example` and recommended volumes | §4 |
| 2.2 | **Example ingest** | Static JSON or minimal mock for testing `SOVEREIGN_INGEST_URL` | §4 |
| 2.3 | **README screenshot/GIF** | Replace placeholder with real Dashboard screenshot or short video | §4 |
| 2.4 | **Queue & Ledger backup** | Docs or scripts for periodic backup of job DB and Ledger | §1 |

**Done when:** New users can bring up the stack from docs; reusable ingest example; first screen clearly shows "can take jobs, observable".

---

## Wave 3: Capability & scale

**Goal:** More workers, higher throughput, optional horizontal scale.

| # | Direction | Action | Ref |
|---|-----------|--------|-----|
| 3.1 | **assistant_chat** | General Q&A; when goal has no clear "write/translate/minutes" etc., Strategist uses `assistant_chat` | §6 |
| 3.2 | **code_assistant / code_review** | Code understanding, edit suggestions, simple code review (LLM only) | §6 |
| 3.3 | **Job concurrency** | Config e.g. `SOVEREIGN_JOB_WORKER_CONCURRENCY=2` for parallel jobs | §1 |
| 3.4 | **Batch job API** | `POST /api/jobs/batch` to submit multiple goals in one call | §6 |
| 3.5 | **Multi-instance queue (optional)** | Redis (or similar) as queue backend for horizontal scaling | §5 |

**Done when:** Conversation-style goals supported; code-related tasks supported; concurrency tunable; batch enqueue; optional multi-instance deployment.

---

## Wave 4: Community & long-term

**Goal:** Clear contribution path, versioning, and outreach.

| # | Direction | Action | Ref |
|---|-----------|--------|-----|
| 4.1 | **Good First Issue** | Tag docs, examples, unit-test issues for new contributors | §8 |
| 4.2 | **Releases & Changelog** | Maintain CHANGELOG by version; tag releases; release notes link to CHANGELOG | §8 |
| 4.3 | **Recovery test (optional)** | Kill process mid-run, restart; verify queue and Ledger restore; optional in CI | §7 |
| 4.4 | **IP allowlist (optional)** | Optional IP allowlist for `POST /api/jobs` or ingest | §3 |

**Done when:** New contributors have a clear entry; users can follow by version; persistence has regression coverage; optional lock-down for enterprise.

---

## Suggested order (one line)

1. **Wave 1:** Graceful shutdown, /health enhancements, request_id, production checks, rate-limit/boundary unit tests.  
2. **Wave 2:** One-click deploy example, ingest example, README screenshot, backup docs.  
3. **Wave 3:** assistant_chat, code_review workers, concurrency, batch API, multi-instance queue as needed.  
4. **Wave 4:** Good First Issue, releases/Changelog, optional recovery test and IP allowlist.

For finer task breakdown or alignment with existing issues, use [OPTIMIZATION_ROADMAP.md](OPTIMIZATION_ROADMAP.md) and filter by priority.
