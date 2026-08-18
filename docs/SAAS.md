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

## Multi-tenant API + console

`saas/api.py` — a standalone FastAPI app (`create_saas_app`), separate from the
single-tenant dashboard. Auth is the tenant API key (`X-Tenant-Key`). Endpoints:

- `POST /saas/tenants` — sign up (name, plan) → returns the tenant + its API key **once**.
- `GET  /saas/tenants/me` — the tenant, its plan, and today's usage (key-authed).
- `PUT  /saas/tenants/me/config` — set BYO keys, charter, earning toggle (secrets masked back).
- `POST /saas/tenants/me/missions` — run a governed mission under the tenant's key/engine;
  402 when a plan limit or missing key blocks it.
- `GET  /` — a minimal signup console (create workspace → save key → configure keys → run).

Each tenant's ledger is seeded once with a monthly operating budget (a governance
ceiling; real LLM cost bills the tenant's own key). Run: `python -m sovereign_os.saas.api`.

## Status & next steps

Done: tenancy core + plan/limit enforcement + per-tenant isolation + BYO-key context;
multi-tenant API + signup console (all validated by the test suite).

Next: (1) marketing site (landing page) + deployment config for a custom domain;
(2) Stripe-billed platform subscriptions (the plan price → real billing); (3) turn on the
earning module for `team` tenants once the live x402/Stripe settlement loop is verified.
