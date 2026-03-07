# Quick Start

Configure **Stripe** and **one LLM API key** (OpenAI or Anthropic) to accept jobs, run basic tasks, and charge.

---

## Three steps

### 1. Clone and install

```bash
git clone https://github.com/YourUsername/Sovereign-OS.git
cd Sovereign-OS
pip install -e ".[llm]"
```

Optional, for Stripe payments:

```bash
pip install -e ".[payments]"
```

### 2. Configure environment

Copy the example env and set **required** keys:

```bash
cp .env.example .env
```

Edit `.env` and set at least:

| Variable | Required | Description |
|----------|----------|-------------|
| `STRIPE_API_KEY` | Yes (for payments) | Stripe secret key; use `sk_test_...` for test |
| `OPENAI_API_KEY` or `ANTHROPIC_API_KEY` | One of them | Used for CEO planning, audit, and built-in workers (summarize, research, reply) |

**Anthropic only:** Set `ANTHROPIC_API_KEY`; you do not need `SOVEREIGN_LLM_PROVIDER`. To pin a model:

```bash
SOVEREIGN_LLM_PROVIDER=anthropic
SOVEREIGN_LLM_MODEL=claude-3-5-sonnet-20241022
```

Optional (recommended for persistence):

- `SOVEREIGN_JOB_DB=./data/jobs.db` — job queue persistence
- `SOVEREIGN_LEDGER_PATH=./data/ledger.jsonl` — ledger persistence
- `SOVEREIGN_AUDIT_TRAIL_PATH=./data/audit.jsonl` — audit trail persistence

**Human-out-of-loop (auto-approve):**

- `SOVEREIGN_AUTO_APPROVE_JOBS=true` — new jobs are approved automatically; with `SOVEREIGN_INGEST_URL` you get full auto ingest → run → charge → webhook.
- `SOVEREIGN_COMPLIANCE_AUTO_PROCEED=true` — when a spend threshold is set, exceeding it still auto-proceeds. See [CONFIG.md](CONFIG.md).

### 3. Start the Web UI

```bash
python -m sovereign_os.web.app
```

Or use project scripts (they load `.env`):

- Windows: `run_paid_demo.bat`
- Or: `docker compose up web`

Open **http://localhost:8000** in your browser.

---

## First job and charge

1. In the Dashboard **Mission** box, enter a goal, e.g. `Summarize the market in one paragraph.`
2. Click **Run**; the system uses the default Charter (`charter.default.yaml`) and built-in workers to plan and run.
3. For **Job queue** payments:
   - Submit a paid job via `POST /api/jobs` or the demo script (see [PAID_DEMO.md](PAID_DEMO.md)).
   - Approve the job in the Dashboard **Job queue**; when it completes, payment is taken and recorded in the Ledger.

---

## Built-in skills

Available out of the box (registered in the default engine):

- `summarize` — summarization
- `research` — short research (bullets + conclusion)
- `reply` — template reply (supports `{{var}}`)
- `write_article` — article writing
- `solve_problem` — problem solving
- `write_email` — email drafting
- `write_post` — social post drafting
- `meeting_minutes` — meeting minutes
- `translate` — translation
- `rewrite_polish` — rewrite and polish
- `collect_info` — information gathering
- `extract_structured` — structured extraction (JSON)
- `spec_writer` — spec / SOW writing

## Health and config warnings

Open **http://localhost:8000/health** to see:

- `status`: service status
- `ledger`: whether the ledger is available
- `config_warnings`: if Stripe or LLM key is missing, listed here so you can fix "silent fallback to Dummy".

---

## How to submit jobs

- **Option 1:** Enter a goal in the Web UI Mission box and click Run (immediate run, no queue).
- **Option 2:** `POST /api/jobs` with body: `goal`, `amount_cents`, `currency`, optional `charter`, `callback_url`; approve in the Dashboard to run and charge.
- **Option 3:** Set `SOVEREIGN_INGEST_URL`; the system polls that JSON URL and enqueues new jobs. See [CONFIG.md](CONFIG.md) and [MONETIZATION.md](MONETIZATION.md).

### Example: submit a paid job with curl

```bash
curl -X POST http://localhost:8000/api/jobs \
  -H "Content-Type: application/json" \
  -d '{"goal":"Summarize the AI market in one paragraph.","amount_cents":100,"currency":"USD"}'
```

If `SOVEREIGN_API_KEY` is set, add the header:

```bash
curl -X POST http://localhost:8000/api/jobs \
  -H "Content-Type: application/json" \
  -H "X-API-Key: YOUR_SOVEREIGN_API_KEY" \
  -d '{"goal":"Research AI trends.","amount_cents":200,"currency":"USD","callback_url":"https://your-server.com/webhook"}'
```

---

## FAQ

| Issue | What to do |
|-------|------------|
| Charges still show Dummy after start | Check `STRIPE_API_KEY` in `.env`; restart the web process; check `config_warnings` in `/health`. |
| Tasks stay Stub with no real output | Set `OPENAI_API_KEY` or `ANTHROPIC_API_KEY` and run `pip install -e ".[llm]"`. |
| Anthropic only | Set `ANTHROPIC_API_KEY`; no need to set `SOVEREIGN_LLM_PROVIDER`. |
| No webhook received | Ensure `SOVEREIGN_WEBHOOK_URL` or the job `callback_url` is reachable; check app logs for webhook retries. |

---

## Next steps

- Charter and workers: [CHARTER.md](CHARTER.md), [WORKER.md](WORKER.md).
- Payments and approval: [MONETIZATION.md](MONETIZATION.md).
- All options: [CONFIG.md](CONFIG.md).
