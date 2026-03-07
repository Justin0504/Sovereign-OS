# Open-Source Ready Plan: Out-of-the-box + Ingest / Delivery / Proactive Contact

Goal: With **Stripe + one LLM API key** configured, users can use built-in workers to accept jobs, run basic tasks, charge, and deliver (including proactive customer notification). This doc is the execution plan; phases can be split into issues/PRs.

---

## 1. Vision and principles

| Principle | Description |
|-----------|-------------|
| **Out-of-the-box** | Clone → set `.env` (Stripe + at least one LLM key) → start → accept jobs, run tasks, charge. |
| **Built-in capabilities** | Built-in workers for summarization, simple research, formatted reply, etc., without writing code first. |
| **Ingest + delivery loop** | API and polling ingest; after task completion, **result callback** and **proactive notification** (webhook) for integration. |
| **Production-grade** | No secrets in config, observable critical paths, retries, traceable delivery (audit trail, Ledger). |

---

## 2. Current capabilities (existing)

| Capability | Status | Notes |
|------------|--------|-------|
| Ingest | ✅ | `POST /api/jobs`; `SOVEREIGN_INGEST_URL` polling |
| Approval | ✅ | Dashboard Job queue Approve; optional compliance threshold |
| Run & audit | ✅ | CEO plan → CFO budget → Registry dispatch → Auditor → TrustScore update |
| Charge & ledger | ✅ | Stripe (test/live), Ledger records income |
| Built-in workers | ✅ | Summarizer, Research, Reply + default Charter (summarize / research / reply) |
| Config | ✅ | `.env` + CONFIG.md; QUICKSTART minimal set; `/health` returns `config_warnings` |
| Delivery / proactive | ✅ | Job completion webhook (`SOVEREIGN_WEBHOOK_URL` + per-job `callback_url`), retry, HMAC signature |

---

## 3. Target state (when ready)

- **User steps:** Copy `.env.example` → set `STRIPE_API_KEY` + `OPENAI_API_KEY` (or `ANTHROPIC_API_KEY`) → run `run_paid_demo.bat` or `docker compose up web`.
- **System:** Uses **default Charter + built-in workers** to accept and run **summarize, simple research, formatted reply**, etc.; charge via Stripe; optional **completion webhook** to push results to your backend; optional "notify customer" (e.g. your backend receives webhook and sends email/SMS).

---

## 4. Phases

### Phase A: Built-in workers + default Charter ("basic tasks deliverable")

**Goal:** Run "ingest → run real task → audit → charge" without writing code.

| # | Task | Output |
|---|------|--------|
| A1 | **Default Charter** | `charter.default.yaml` in repo; skills match built-in workers: summarize, research, reply. |
| A2 | **Built-in worker registry** | At engine/Web startup, register summarize → SummarizerWorker, research → ResearchWorker, reply → ReplyWorker. |
| A3 | **Strategist alignment** | CEO only emits registered skills (or falls back to Stub); doc lists default skills. |
| A4 | **Deps & docs** | `pip install -e .` or `pip install -e ".[llm]"`; README states "min config: STRIPE_API_KEY + OPENAI_API_KEY". |

**Done when:** New user clone → set 2 keys → start → submit "Summarize …" → Approve → task completes with SummarizerWorker and charge.

---

### Phase B: Minimal config + startup checks ("out-of-the-box")

**Goal:** User only sets Stripe + one LLM key; startup gives clear warnings so "silent fallback to Dummy" is visible.

| # | Task | Output |
|---|------|--------|
| B1 | **Minimal config doc** | `docs/QUICKSTART.md`: three steps (clone, set `.env`, start); required: `STRIPE_API_KEY`, `OPENAI_API_KEY` or `ANTHROPIC_API_KEY`; optional: `SOVEREIGN_JOB_DB`, `SOVEREIGN_LEDGER_PATH`. |
| B2 | **Startup config check** | At Web start (or first `/health`): if Stripe or LLM key missing, WARNING or 200 + `config_warnings: ["STRIPE_API_KEY not set", "No LLM key set"]`. |
| B3 | **.env.example** | Mark required/optional in `.env.example` and note "copy to .env and set the two keys to accept jobs and charge". |

**Done when:** Docs and comments match; missing keys produce clear log or health hint.

---

### Phase C: Delivery & proactive contact (webhook + callback)

**Goal:** When a task/job completes, the system **actively** pushes results to the user or client system.

| # | Task | Output |
|---|------|--------|
| C1 | **Job completion webhook (global)** | Env `SOVEREIGN_WEBHOOK_URL` (optional). When job becomes `completed` or `payment_failed`, POST once to that URL. Payload spec below. Header `X-Sovereign-Signature` (HMAC-SHA256 of body, key `SOVEREIGN_WEBHOOK_SECRET`) for verification. |
| C2 | **Webhook retry & log** | Retry 2–3 times on failure (backoff); log request/response; optional write to `data/webhook_log.jsonl`. |
| C3 | **Per-job callback_url (optional)** | If `POST /api/jobs` body supports `callback_url`, on completion POST to `callback_url` first, else use global `SOVEREIGN_WEBHOOK_URL`. Same payload as C1. |
| C4 | **Docs** | Add "delivery & proactive contact" in CONFIG.md, MONETIZATION.md: webhook config, payload format, signature; typical "notify customer" (backend receives webhook then email/SMS). |

**Done when:** With `SOVEREIGN_WEBHOOK_URL` set, user backend receives POST on job completion; docs allow third parties to implement "proactive contact".

---

### Phase D: Ingest & queue (production-grade)

**Goal:** Clear ingest options, observable, rate-limited.

| # | Task | Output |
|---|------|--------|
| D1 | **Ingest docs** | In README or QUICKSTART: ① direct `POST /api/jobs`; ② set `SOVEREIGN_INGEST_URL` for polling; ③ optional `SOVEREIGN_API_KEY`. Include curl and body examples. |
| D2 | **Ingest dedup** | If polled job duplicates existing (goal, amount_cents, created_ts window), skip or mark duplicate (optional). |
| D3 | **Rate limit & observability** | Simple rate limit on `POST /api/jobs` (e.g. N/min by IP or API key); expose queue length, today’s ingest count in `/health` or `/metrics` (reuse Prometheus if present). |

**Done when:** Docs complete; no duplicate execution under load; observable; rate limit available.

---

### Phase E: Tests, docs, release

**Goal:** New-user path has test coverage; docs consistent; version publishable.

| # | Task | Output |
|---|------|--------|
| E1 | **E2E test** | One E2E: start app (or test client) → create job → Approve → assert completed, Ledger has job_income, if webhook enabled mock receives POST. |
| E2 | **QUICKSTART & README** | README "min three steps" points to QUICKSTART; QUICKSTART: Stripe + LLM key, first job, Stripe & Dashboard. |
| E3 | **Version & release** | After phases merge, tag (e.g. v0.3.0); CHANGELOG: built-in workers, default Charter, webhook delivery, minimal config, doc updates. |

---

## 5. Built-in worker spec (Phase A detail)

| Skill ID | Worker | Input | Output | Deps |
|----------|--------|-------|--------|------|
| `summarize` | SummarizerWorker | task.description | Summary paragraph | LLM |
| `research` | ResearchWorker | topic/question | Bullets + conclusion | LLM |
| `reply` | ReplyWorker | template + \|\| key=value | Filled reply | Template / LLM |
| `write_article` | ArticleWriterWorker | topic/audience/tone/length | Outline/draft/bullets | LLM |
| `solve_problem` | ProblemSolverWorker | task.description (problem) | Steps → answer | LLM |
| `write_email` | EmailWriterWorker | to/purpose/tone | Subject x3 + body | LLM |
| `write_post` | SocialPostWorker | platform/audience | Post variants + CTA | LLM |
| `meeting_minutes` | MeetingMinutesWorker | task.description (notes) | Decisions/actions/risks | LLM |
| `translate` | TranslateWorker | target_language/style | Translation (format preserved) | LLM |
| `rewrite_polish` | RewritePolishWorker | goal/tone | Polished text + change notes | LLM |
| `collect_info` | InfoCollectorWorker | depth/format | Research plan + interim + checklist | LLM |
| `extract_structured` | ExtractStructuredWorker | task.description + schema | JSON + missing fields | LLM |
| `spec_writer` | SpecWriterWorker | task.description | SOW: scope/deliverables/acceptance/risks | LLM |

Default Charter (`charter.default.yaml`) `core_competencies` lists all of the above; registry registration matches. See [WORKER.md](WORKER.md) and [QUICKSTART.md](QUICKSTART.md).

---

## 6. Proactive contact and "notify customer"

- **System:** Only "at the right time, send structured result to the user’s endpoint" — i.e. **job completion webhook** (and optional per-job `callback_url`). No built-in email/SMS (avoids deps and privacy).
- **User:** Their service receives the webhook and then: store in DB, update ticket; call email/SMS/Slack API to "proactively contact customer"; or show `result_summary` in their UI.

So "proactive contact" is implemented by the user’s system; this system only does reliable, retriable **push**.

---

## 6.1 Webhook payload spec

```json
{
  "job_id": "string",
  "status": "completed | payment_failed",
  "goal": "User-submitted goal summary",
  "amount_cents": 100,
  "currency": "USD",
  "payment_id": "ch_xxx or null",
  "completed_at": "ISO8601",
  "result_summary": "Task output summary, recommend ≤2KB",
  "audit_score": 0.9,
  "charter": "Default"
}
```

- Receiver should handle idempotently (same `job_id` multiple times → process once).
- Optional: when `SOVEREIGN_WEBHOOK_SECRET` is set, header `X-Sovereign-Signature: sha256=<hex>` is HMAC of raw JSON body.

---

## 7. Config overview (required/optional when ready)

| Config | Required | Description |
|--------|----------|-------------|
| `STRIPE_API_KEY` | Yes (for charge) | Stripe secret; use `sk_test_` for test. |
| `OPENAI_API_KEY` or `ANTHROPIC_API_KEY` | At least one (for built-in LLM workers) | CEO, audit, Summarizer, Research, etc. |
| `SOVEREIGN_JOB_DB` | Recommended | Persistent queue. |
| `SOVEREIGN_LEDGER_PATH` | Recommended | Persistent ledger. |
| `SOVEREIGN_WEBHOOK_URL` | Optional | POST delivery on job completion. |
| `SOVEREIGN_INGEST_URL` | Optional | Polling ingest. |
| `SOVEREIGN_API_KEY` | Optional | Protect `POST /api/jobs`. |

---

## 8. Suggested implementation order

1. **Phase A:** Default Charter + register Summarizer/Research/Reply → "basic tasks deliverable".  
2. **Phase B:** QUICKSTART + startup checks + .env.example → "out-of-the-box" and visible config issues.  
3. **Phase C:** Webhook + per-job callback_url (optional) → "delivery & proactive contact".  
4. **Phase D:** Ingest docs, dedup/rate limit/observability as needed.  
5. **Phase E:** E2E tests + doc freeze + release.

Result: "Configure Stripe + API key → accept jobs, deliver basic tasks, charge" loop first; then webhook and docs for "proactive contact"; finally production-grade, out-of-the-box open source.

---

## 8.1 Progress (v0.3.0 done)

| Phase | Status | Notes |
|-------|--------|-------|
| Phase A | ✅ | charter.default.yaml; Summarizer/Research/Reply built-in and registered; Web loads default charter |
| Phase B | ✅ | QUICKSTART.md; `/health` has config_warnings; .env.example comments |
| Phase C | ✅ | SOVEREIGN_WEBHOOK_URL, callback_url, retry, X-Sovereign-Signature; CONFIG/MONETIZATION docs |
| Phase D | ✅ | Ingest in QUICKSTART; `/health` exposes jobs_total, jobs_pending |
| Phase E | ✅ | E2E and webhook tests; CHANGELOG v0.3.0; README points to QUICKSTART |

**Next (optimization):** Phase D2 ingest dedup (optional `SOVEREIGN_INGEST_DEDUP_SEC`); Phase D3 rate limit and `/metrics`; webhook failure logging; more workers. Full list: [OPTIMIZATION_ROADMAP.md](OPTIMIZATION_ROADMAP.md).

---

## 9. Security and secrets (production requirements)

| Item | Requirement |
|------|-------------|
| No secrets in repo | Read only via env or secret manager; no real keys in code/README. |
| .env not committed | `.gitignore` includes `.env`; `.env.example` placeholders only. |
| Webhook verification | Receiver should verify `X-Sovereign-Signature` when `SOVEREIGN_WEBHOOK_SECRET` is set. |
| API protection | Production: set `SOVEREIGN_API_KEY` to avoid unauthorized `POST /api/jobs`. |
| Stripe webhook | If handling Stripe events, verify `Stripe-Signature` (use `STRIPE_WEBHOOK_SECRET`). |
