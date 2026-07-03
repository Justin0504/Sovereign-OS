# Metrics & Observability

Sovereign-OS exports Prometheus metrics for cost, throughput, and — as of the
governance-hardening work — the three guardrail systems: the **CFO circuit
breaker**, **JIT capability leases**, and **per-category audit quality**. Point
Grafana at the scrape endpoint and you get long-term dashboards over the same
signals the web *Guardrails* tab and the terminal Command Center show live.

## Scrape endpoints

Two ways to expose `/metrics` (both serve the same Prometheus text format):

- **Web dashboard** — the FastAPI app serves `GET /metrics` on its own port
  (`http://localhost:8000/metrics`). Governance gauges (breaker state, active
  leases, agent trust) are refreshed on every scrape, so values are always current.
- **Standalone server** — set `SOVEREIGN_PROMETHEUS_PORT` (e.g. `9464`) before
  launching the TUI (`python -m sovereign_os.ui.app`) or any long-running process;
  `init_telemetry` starts a dedicated metrics server. Histograms and the breaker
  gauges are updated as missions run.

`prometheus_client` must be installed (it ships with the `[llm]`/`[dev]` extras).
Without it, `/metrics` returns a stub and all recorders are no-ops.

## Metric reference

### Cost & throughput (existing)

| Metric | Type | Labels | Meaning |
|---|---|---|---|
| `sovereign_tokens_total` | counter | `model` | Total tokens (input+output) per model |
| `sovereign_audit_success_total` | counter | `model` | Audit passes per judge model |
| `sovereign_audit_fail_total` | counter | `model` | Audit fails per judge model |
| `sovereign_jobs_completed_total` | counter | `status` | Jobs finished, by final status |
| `sovereign_job_duration_seconds` | histogram | — | Job execution time |
| `sovereign_jobs_pending` | gauge | — | Jobs awaiting approval |
| `sovereign_jobs_running` | gauge | — | Jobs currently running |

### CFO circuit breaker

| Metric | Type | Labels | Meaning |
|---|---|---|---|
| `sovereign_breaker_session_spend_cents` | gauge | — | Cumulative session spend |
| `sovereign_breaker_session_revenue_cents` | gauge | — | Cumulative session revenue |
| `sovereign_breaker_session_ceiling_cents` | gauge | — | Configured session ceiling (0 = off) |
| `sovereign_breaker_consecutive_failures` | gauge | — | Current audit-failure streak |
| `sovereign_breaker_roi` | gauge | — | Realized ROI (revenue/spend); `-1` when undefined |
| `sovereign_breaker_tripped` | gauge | — | `1` if the breaker is currently tripped |
| `sovereign_breaker_trips_total` | counter | `reason` | Trip count; `reason` ∈ {ceiling, failure_streak, roi, other} |

### JIT permissions & trust

| Metric | Type | Labels | Meaning |
|---|---|---|---|
| `sovereign_active_leases` | gauge | — | Active JIT capability leases (total) |
| `sovereign_agent_trust_score` | gauge | `agent` | TrustScore (0–100) per agent |

### Audit quality (per-category rubric)

| Metric | Type | Labels | Meaning |
|---|---|---|---|
| `sovereign_audit_score` | histogram | `category` | Overall audit score, per work category |
| `sovereign_audit_criterion_score` | histogram | `category`, `criterion` | Rubric criterion score (correctness/robustness/clarity/…) |

Buckets for both audit histograms: `0.1, 0.3, 0.5, 0.7, 0.85, 0.95, 1.0`.

### Autonomous profitability & self-repair

| Metric | Type | Labels | Meaning |
|---|---|---|---|
| `sovereign_tasks_screened_total` | counter | `decision` | Ingest profit-screen decisions; `decision` ∈ {take, skip} |
| `sovereign_task_repairs_total` | counter | `outcome` | Reactive self-repair results; `outcome` ∈ {recovered, exhausted} |

## Configuration

The breaker limits (which also drive the `_ceiling` / trip metrics) are set via env:

```bash
SOVEREIGN_SESSION_CEILING_CENTS=500     # halt when session spend hits $5.00
SOVEREIGN_MAX_CONSECUTIVE_FAILURES=3    # halt after 3 failed audits in a row
SOVEREIGN_ROI_FLOOR=0.5                 # halt when ROI drops below 0.5
SOVEREIGN_ROI_GRACE_CENTS=200           # ...but only after $2.00 spent
SOVEREIGN_PROMETHEUS_PORT=9464          # standalone metrics server (TUI/worker)
```

All default off → the metrics still export (pure observability) but nothing trips.

## Grafana starter panels (PromQL)

```promql
# Session spend vs. ceiling (%). Alert when > 80.
100 * sovereign_breaker_session_spend_cents / clamp_min(sovereign_breaker_session_ceiling_cents, 1)

# Breaker trips in the last hour, by reason
increase(sovereign_breaker_trips_total[1h])

# Median audit score per category (from the histogram)
histogram_quantile(0.5, sum by (category, le) (rate(sovereign_audit_score_bucket[5m])))

# Weakest rubric criterion per category (p10)
histogram_quantile(0.1, sum by (category, criterion, le) (rate(sovereign_audit_criterion_score_bucket[30m])))

# Fraction of audits scoring >= 0.85 (right-tail quality)
sum(rate(sovereign_audit_score_bucket{le="0.85"}[15m])) by (category)
  / sum(rate(sovereign_audit_score_count[15m])) by (category)

# Active JIT leases (should trend to 0 between missions)
sovereign_active_leases

# Agents below a trust floor (candidates for review)
sovereign_agent_trust_score < 40
```

Suggested alerts: `sovereign_breaker_tripped == 1` (page), session-spend > 80% of
ceiling (warn), and a sustained drop in `histogram_quantile(0.5, … audit_score …)`
for any category (quality regression).
