# Sovereign-OS as a SaaS (multi-tenant)

The SaaS sells the **governed agent control plane** — budget control, earned-autonomy
permissions, tamper-evident audit, and verified delivery — not a cut of any "earnings."
This keeps the MVP out of the money-transmission / custody / KYC swamp:

- **Bring your own keys.** Each tenant supplies its own LLM key and, optionally, its own
  Stripe account and wallet. The platform never holds funds or custodies keys on a
  tenant's behalf — a tenant's usage bills the tenant's own key.
- **Subscription, not commission.** Plans gate platform capability and safety limits
  (`saas/plans.py`), not access to money.
- **Earning is an opt-in module.** The marketplace/earning loop is a `team`-plan feature,
  off by default until the live-settlement loop is proven, so the product never
  over-promises.

## Tenant isolation

`saas/tenancy.py` — `Tenant`, `TenantConfig`, and a JSON-backed `TenantStore`:

- API-key auth (`by_api_key`, constant-time compare).
- Per-tenant data directory `<root>/<tenant_id>/` holding an isolated ledger, trust
  store, audit trail, and jobs — one tenant can never see another's state.
- Per-day usage meter with plan limits (`can_run`): requires a BYO LLM key, enforces a
  daily mission count and a daily spend ceiling.
- `redacted()`/`public()` never return secrets.

`saas/runtime.py`:

- `build_tenant_engine(tenant, store)` constructs a fully-isolated `GovernanceEngine`
  (its own persisted ledger/trust/audit, its own charter, a plan-sized circuit breaker).
- `tenant_llm_context(tenant)` binds the tenant's BYO keys for the duration of a mission
  via a `contextvars` key context in `llm/providers.py` — so workers use the tenant's
  key, never a shared platform key, safely under async concurrency.
- `run_tenant_mission(...)` enforces plan limits, runs under the tenant's keys/engine,
  and meters realized token spend.

## Status & next steps

Done: tenancy core, plan/limit enforcement, per-tenant isolation, BYO-key context
(validated by the test suite).

Next: (1) multi-tenant web routing — resolve the tenant from an API key / subdomain and
scope the dashboard + API to that tenant; (2) a signup/console UI and Stripe-billed
subscriptions for the platform itself; (3) turn on the earning module for `team` tenants
once the live x402/Stripe settlement loop is verified end-to-end.
