# Examples

Run a full mission in under a minute: Charter → plan → CFO approval → execution → audit.

**Default charter:** When you start the Web UI without `--charter`, it loads `charter.default.yaml` and uses built-in workers (no extra code):  
`summarize`, `research`, `reply`, `write_article`, `solve_problem`, `write_email`, `write_post`, `meeting_minutes`, `translate`, `rewrite_polish`, `collect_info`, `extract_structured`, `spec_writer`.

## 1. One-shot mission (CLI)

From the **project root** (parent of `examples/`):

```bash
pip install -e .
sovereign run --charter charter.example.yaml "Summarize the market in one paragraph."
```

You get: task plan, CFO approval, execution, and an audit report. No ledger/audit files by default.

## 2. Mission with ledger and audit trail (reproducible)

Persist spending and audits so you can inspect them later:

```bash
# Windows (PowerShell or cmd)
demo.bat

# Linux / macOS
./demo.sh
```

Or manually:

```bash
sovereign run --charter charter.example.yaml --ledger ./data/ledger.jsonl --audit-trail ./data/audit.jsonl "Summarize the market in one paragraph."
# Inspect:
#   type data\audit.jsonl     (Windows)
#   cat data/audit.jsonl      (Linux/macOS)
```

Each line in `audit.jsonl` is an `AuditReport` with `proof_hash`; see [AUDIT_PROOF.md](../docs/AUDIT_PROOF.md) to verify integrity.

## 3. Freelancer-style charter

Use a charter tuned for “bounty-style” work (research + delivery, strict cost per task):

```bash
sovereign run --charter examples/freelancer.yaml "Draft a one-page competitive analysis."
```

See `examples/freelancer.yaml` for mission, competencies, and KPIs.

## 4. Web Dashboard (run mission from the UI)

```bash
python -m sovereign_os.web.app
# Open http://localhost:8000
# Enter a goal and click Run; watch tasks, health, token usage, and audit trail.
```

To persist audit trail from the Web UI, set before starting:

```bash
set SOVEREIGN_AUDIT_TRAIL_PATH=./data/audit.jsonl   # Windows
export SOVEREIGN_AUDIT_TRAIL_PATH=./data/audit.jsonl  # Linux/macOS
python -m sovereign_os.web.app
```

## 5. E2E test (no real API)

No API keys needed; uses mock CEO and stub workers:

```bash
pip install -e ".[dev]"
pytest tests/test_e2e_pipeline.py -v
```

Asserts: plan → CFO approval → dispatch → audit and `proof_hash` present.

## 6. Example Ingest (SOVEREIGN_INGEST_URL)

To test the ingest poller with a static JSON file:

1. Serve the example file (from project root):
   ```bash
   python -m http.server 8888 --directory examples
   ```
2. Set env and start Web UI:
   ```bash
   set SOVEREIGN_INGEST_URL=http://localhost:8888/ingest_example.json
   set SOVEREIGN_INGEST_INTERVAL_SEC=30
   python -m sovereign_os.web.app
   ```
3. Within 30s the poller will fetch `ingest_example.json` and enqueue the two jobs. Adjust `ingest_example.json` to your needs; format: array of `{ "goal", "charter?", "amount_cents?", "currency?" }`.

## 7. Paid job demo (Stripe 测试模式「赚钱」)

演示完整流程：接单 → 人工批准 → 执行 → 审计通过 → Stripe 扣款 → 收入记入 Ledger。

**1. 准备 Stripe 测试密钥**

- 登录 [Stripe Dashboard](https://dashboard.stripe.com)，打开 **Test mode**。
- **Developers → API keys** 复制 Secret key（`sk_test_...`）。

**2. 启动 Web UI（带 Job 持久化与 Ledger）**

```powershell
# Windows PowerShell（项目根目录）
$env:SOVEREIGN_JOB_DB = ".\data\jobs.db"
$env:SOVEREIGN_LEDGER_PATH = ".\data\ledger.jsonl"
$env:STRIPE_API_KEY = "sk_test_你的密钥"
python -m sovereign_os.web.app
```

**3. 提交一笔付费任务**

- 浏览器打开 http://localhost:8000，或使用脚本创建 Job：

```powershell
.\examples\demo_paid_job.ps1
```

- 或用 curl：  
  `curl -X POST http://localhost:8000/api/jobs -H "Content-Type: application/json" -d "{\"goal\":\"Summarize the market.\",\"amount_cents\":100,\"currency\":\"USD\"}"`  
- 或在 PowerShell 里：  
  `Invoke-RestMethod -Uri "http://localhost:8000/api/jobs" -Method POST -ContentType "application/json" -Body '{"goal":"Summarize the market.","amount_cents":100,"currency":"USD"}'`

**4. 在 Dashboard 里批准并观察**

- **Job queue** 里出现该任务（pending）→ 点击 **Approve**。
- 系统跑 mission → 审计通过后调用 Stripe 扣款（测试卡不会真扣钱）→ 收入写入 Ledger。
- 在 Dashboard 看 **Balance** 和 **Token usage**；Ledger 中会有 `job_income` 记录。

详见 [MONETIZATION.md](../docs/MONETIZATION.md)。
