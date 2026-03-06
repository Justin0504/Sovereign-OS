# End-to-end example

One complete run: load a Charter, execute a mission, and inspect the result.

## 1. Charter

Use the example Charter in the repo (or your own):

```yaml
# charter.example.yaml
mission: |
  Operate as an autonomous research and delivery agent...
core_competencies:
  - name: research
    description: Web and document research, synthesis
    priority: 8
fiscal_boundaries:
  daily_burn_max_usd: 50.00
  max_budget_usd: 2000.00
success_kpis:
  - name: task_ok
    verification_prompt: "Did the output satisfy the task?"
```

## 2. Run a mission (CLI)

```bash
pip install -e .
sovereign run --charter charter.example.yaml "Summarize the market in one paragraph."
```

Output (conceptually):

```
Tasks: 1, Passed: 1/1
  task-1: OK — Summary of the market...
```

## 3. Run a mission (Python)

```python
import asyncio
from sovereign_os import load_charter, UnifiedLedger
from sovereign_os.auditor import ReviewEngine
from sovereign_os.governance import GovernanceEngine
from sovereign_os.agents import SovereignAuth

async def main():
    charter = load_charter("charter.example.yaml")
    ledger = UnifiedLedger()
    ledger.record_usd(10_00)  # $10.00
    engine = GovernanceEngine(
        charter, ledger,
        auth=SovereignAuth(),
        review_engine=ReviewEngine(charter),
    )
    plan, results, reports = await engine.run_mission_with_audit("Summarize the market.")
    print(f"Tasks: {len(plan.tasks)}, Passed: {sum(1 for r in reports if r.passed)}/{len(reports)}")
    for r in results:
        print(f"  {r.task_id}: {'OK' if r.success else 'FAIL'} — {r.output[:80]}")

asyncio.run(main())
```

## 4. Run via Web UI

```bash
python -m sovereign_os.web.app
# Open http://localhost:8000
# Click Run or POST /api/run with {"goal": "Summarize the market."}
# Watch tasks and logs on the dashboard.
```

## 5. Inspect result

- **Ledger:** `ledger.total_usd_cents()`, `ledger.entries()`, `ledger.total_tokens_by_model()`
- **Audit:** each `AuditReport` has `passed`, `score`, `reason`, `suggested_fix`
- **Tasks:** each `TaskResult` has `task_id`, `success`, `output`

That’s one full cycle: Charter → plan → CFO approval → dispatch → audit → result.

## 6. Reproducible E2E (no real API)

To verify the full pipeline without calling any LLM API, run the integration test (mock CEO, stub workers, no network):

```bash
pytest tests/test_e2e_pipeline.py -v
```

This runs: plan → CFO approval → dispatch → audit; asserts `AuditReport.proof_hash` is present and audit trail is written when `audit_trail_path` is set. For a one-shot mission with audit trail persistence:

```bash
sovereign run --charter charter.example.yaml --ledger ./data/ledger.jsonl --audit-trail ./data/audit.jsonl "Summarize the market."
# Then inspect: cat ./data/audit.jsonl
```
