# Changelog

All notable changes to Sovereign-OS are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [Unreleased]

(No changes yet.)

## [0.3.0] — 2026-03-06

### Added

- **Default charter & built-in workers (Phase A):** `charter.default.yaml` with skills `summarize`, `research`, `reply`. Web UI prefers it when no charter path is given. Engine default registry registers `SummarizerWorker`, `ResearchWorker`, `ReplyWorker` so users get working tasks without writing code.
- **ResearchWorker & ReplyWorker:** New built-in workers: `ResearchWorker` (LLM short research, bullet points + conclusion), `ReplyWorker` (template fill with `{{var}}` placeholders, optional LLM polish). Exported from `sovereign_os.agents`.
- **LLM provider auto-detection:** If only `ANTHROPIC_API_KEY` is set (no `OPENAI_API_KEY`), default provider is `anthropic` with model `claude-3-5-sonnet-20241022`. See [QUICKSTART](docs/QUICKSTART.md).
- **Job completion webhook (Phase C):** `SOVEREIGN_WEBHOOK_URL` and per-job `callback_url` in `POST /api/jobs`. When a job becomes `completed` or `payment_failed`, a JSON payload is POSTed (job_id, status, goal, result_summary, audit_score, etc.) with optional HMAC via `SOVEREIGN_WEBHOOK_SECRET`. Retries with backoff. See [CONFIG](docs/CONFIG.md) and [MONETIZATION](docs/MONETIZATION.md).
- **Health config warnings:** `GET /health` now includes `config_warnings` (e.g. missing Stripe or LLM key) so operators can see setup issues. Also `jobs_total` and `jobs_pending` for queue observability.
- **docs/QUICKSTART.md:** Three-step quick start (clone, configure Stripe + one LLM key, run). Documents Anthropic-only setup and optional webhook/callback_url.
- **.env.example:** Comments for required vs optional; webhook and LLM provider placeholders.

### Changed

- **Version:** 0.2.0 → 0.3.0.
- Charter resolution order: `charter.default.yaml` is preferred over `charter.example.yaml` when no path is passed.
- Job model and JobStore: added `callback_url` (optional). `POST /api/jobs` accepts `callback_url` in body.

## [0.2.0] — 2026-03-06

### Added

- **README first screen:** One-liner at top, badges (CI, License, Python), 3-step Quick Start; image placeholder; [examples/](examples/) and Web Dashboard linked.
- **examples/:** [examples/README.md](examples/README.md) with CLI, ledger+audit-trail, freelancer charter; `demo.sh` and `demo.bat` for one-command run with `--ledger` and `--audit-trail`.
- **docs/MONETIZATION.md:** Job queue, Stripe, human approval, and compliance threshold; end-to-end monetization flow.
- **docs/RELEASE.md:** Release checklist and suggested HN/Reddit post text.
- **Compliance hook (Phase 6b):** `HumanApprovalRequiredError`; `Treasury` accepts `compliance_hook` and `spend_threshold_cents`; when task cost ≥ threshold, hook can return `REQUEST_HUMAN_APPROVAL` and mission raises. `GovernanceEngine` passes hook to Treasury. Web UI: set `SOVEREIGN_COMPLIANCE_SPEND_THRESHOLD_CENTS` to enable; on raise, job is set back to `pending` with message. `ThresholdComplianceHook` in `sovereign_os.compliance`. See [PHASE6](docs/PHASE6.md) and [CONFIG](docs/CONFIG.md).
- **CONFIG:** `SOVEREIGN_COMPLIANCE_SPEND_THRESHOLD_CENTS` documented.

### Changed

- **Version:** 0.1.0 → 0.2.0.

## [0.1.0] — earlier

- Core: Charter, UnifiedLedger, GovernanceEngine (Strategist, Treasury), SovereignAuth, WorkerRegistry, ReviewEngine, MemoryManager.
- MCP: client, tool graph, MCPWorker, self-hiring (Phase 5).
- Web UI: dashboard with health, token usage, job queue, mission run.
- CLI: `sovereign run`, `--version`, `--ledger`, `--audit-trail`.
- 24/7: Job ingest, human approval, Stripe payment integration.
- Tests and CI: pytest, GitHub Actions.
