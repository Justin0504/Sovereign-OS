# CEO & CFO: Economic Logic and Profitability

Sovereign-OS applies **top-company CEO/CFO practices** so the system has real economic discipline: it rejects unprofitable jobs and respects margin floors.

## Principles (from top-company practice)

- **Unit economics**: Every paid job has revenue (job price) and cost (estimated task execution). Gross margin = (revenue - cost) / revenue. Top companies (e.g. SaaS, hyperscalers) target high gross margins and enforce **minimum margin floors** so they do not accept loss-making deals.
- **CFO guardrails**: CFOs use metrics like gross margin, runway, and burn rate. Here, the CFO enforces:
  - **Budget**: Balance and daily burn cap (existing behavior).
  - **Profitability**: For each paid job, estimated cost must not exceed `revenue × (1 - min_job_margin_ratio)`. So a 20% margin floor means cost ≤ 80% of revenue; otherwise the job is rejected.
- **CEO–CFO alignment**: The CEO (Strategist) produces the plan and cost estimate; the CFO (Treasury) checks both liquidity and **per-job unit economics**. If the plan would make the job unprofitable, the CFO aborts before any spend.

## How it works in Sovereign-OS

1. **Charter** (`fiscal_boundaries.min_job_margin_ratio`): Optional, 0–1. Example: `0.2` = 20% minimum gross margin (cost must be ≤ 80% of job revenue). Set to `0` to disable (only balance/daily cap apply).
2. **When a job runs**: The app passes `job.amount_cents` (revenue) into `run_mission_with_audit(..., job_revenue_cents=job.amount_cents)`.
3. **After the CEO plan**: The engine sums `total_estimated_cents` (cost). The CFO then runs `approve_job_profitability(job_revenue_cents, total_estimated_cents)`. If `total_estimated_cents > job_revenue_cents × (1 - min_job_margin_ratio)`, it raises `UnprofitableJobError` and the job is not executed.
4. **UI**: If the job is rejected for profitability, the Decision stream shows a CFO message and the job status is set to failed with a clear error.

## Configuration

In your Charter YAML (e.g. `charter.default.yaml`):

```yaml
fiscal_boundaries:
  daily_burn_max_usd: 50.00
  max_budget_usd: 2000.00
  currency: USD
  min_job_margin_ratio: 0.2   # 20% min margin; reject job if cost > 80% of revenue
```

- `min_job_margin_ratio: 0` — profitability check disabled; only balance and daily cap apply.
- `min_job_margin_ratio: 0.2` — 20% margin floor (typical for many B2B / services).
- `min_job_margin_ratio: 0.3` — 30% margin floor (stricter).

## Example

- Job price (revenue): $80 → 8000 cents.
- CEO plan: 2 tasks, total estimated cost 7000 cents.
- Margin = (8000 - 7000) / 8000 = 12.5%.
- If `min_job_margin_ratio: 0.2`, then 12.5% < 20% → CFO rejects the job as unprofitable.

So the system **only runs paid jobs that meet the configured margin floor**, giving it a real “economic brain” aligned with how top-company CEOs and CFOs think about profitability.
