# One-click deploy (Docker Compose)

Run Sovereign-OS Web UI 24/7 with persistent queue and ledger.

## 1. Clone and configure

```bash
git clone https://github.com/Justin0504/Sovereign-OS.git
cd Sovereign-OS
cp .env.example .env
```

Edit `.env`: set at least `STRIPE_API_KEY` and one of `OPENAI_API_KEY` or `ANTHROPIC_API_KEY`. See [QUICKSTART](QUICKSTART.md) and [CONFIG](CONFIG.md).

## 2. Start with Docker Compose

```bash
docker compose up -d redis web
```

- **redis**: optional; used for health check and future cache.
- **web**: Web UI on port 8000, job worker, optional ingest poller.

Open **http://localhost:8000**. Jobs and ledger persist in the `sovereign_data` volume.

## 3. Volume mounts (recommended)

Default compose already mounts:

- `sovereign_data` → `/app/data` (ledger, job DB, audit trail when configured)

To use a host directory instead:

```yaml
# docker-compose.override.yml
services:
  web:
    environment:
      - SOVEREIGN_LEDGER_PATH=/app/data/ledger.jsonl
      - SOVEREIGN_JOB_DB=/app/data/jobs.db
    volumes:
      - ./data:/app/data
```

Then create `./data` and run `docker compose up -d web`.

## 4. Graceful shutdown

On `SIGTERM` (e.g. `docker stop`), the process waits for the current job to finish (up to 120s) before exiting. No need to force-kill.

## 5. Health and observability

- **Health:** `GET http://localhost:8000/health` — status, ledger, redis, `jobs_total`, `jobs_pending`, `jobs_running`, `last_job_completed_at`, `config_warnings`.
- **API docs:** http://localhost:8000/docs

Use `/health` in your load balancer or orchestrator for readiness checks.
