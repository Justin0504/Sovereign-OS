"""
Tenant runtime: build an isolated governance engine per tenant and run missions
under the tenant's own LLM keys.

Isolation is by construction — each tenant gets its own persisted ledger, trust
store, and audit trail under its data dir, its own charter, and a spend circuit
breaker sized to its plan. Missions run inside `tenant_llm_context` so the tenant's
BYO keys (never a shared platform key) bill the tenant's account.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any

from sovereign_os.saas.plans import get_plan
from sovereign_os.saas.tenancy import Tenant, TenantStore


@contextmanager
def tenant_llm_context(tenant: Tenant):
    """Bind the tenant's BYO LLM keys for the duration of a mission."""
    from sovereign_os.llm.providers import reset_tenant_keys, set_tenant_keys

    tok = set_tenant_keys(tenant.config.llm_keys())
    try:
        yield
    finally:
        reset_tenant_keys(tok)


def tenant_earning_active(tenant: Tenant) -> bool:
    """Earning/marketplace runs only when the plan allows it AND the tenant opted in."""
    return tenant.has_feature("earning") and bool(tenant.config.earning_enabled)


def _charter_for(tenant: Tenant):
    from sovereign_os.models.charter import Charter

    raw = (tenant.config.charter_yaml or "").strip()
    if raw:
        try:
            import yaml

            return Charter.model_validate(yaml.safe_load(raw) or {})
        except Exception:  # noqa: BLE001 - fall back to a default charter
            pass
    return Charter(mission=f"{tenant.name} — governed AI agent workspace.")


def build_tenant_engine(tenant: Tenant, store: TenantStore) -> tuple[Any, Any, Any]:
    """Construct a fully-isolated (engine, ledger, auth) for one tenant."""
    from sovereign_os.agents.auth import SovereignAuth
    from sovereign_os.auditor import ReviewEngine
    from sovereign_os.governance.circuit_breaker import SpendCircuitBreaker
    from sovereign_os.governance.engine import GovernanceEngine
    from sovereign_os.ledger.unified_ledger import UnifiedLedger

    d = store.data_dir(tenant)
    d.mkdir(parents=True, exist_ok=True)
    charter = _charter_for(tenant)
    ledger = UnifiedLedger(persist_path=str(d / "ledger.jsonl"))
    auth = SovereignAuth(persist_path=str(d / "trust.json"))
    review = ReviewEngine(charter, audit_trail_path=str(d / "audit.jsonl"))
    breaker = SpendCircuitBreaker(session_ceiling_cents=get_plan(tenant.plan).max_daily_spend_cents)
    engine = GovernanceEngine(charter, ledger, auth=auth, review_engine=review, circuit_breaker=breaker)
    return engine, ledger, auth


def _spend_cents(ledger: Any) -> int:
    fn = getattr(ledger, "total_token_estimated_usd_cents", None)
    try:
        return int(fn()) if callable(fn) else 0
    except Exception:
        return 0


async def run_tenant_mission(tenant: Tenant, store: TenantStore, goal: str, *,
                             job_revenue_cents: int | None = None, max_repair_attempts: int = 0):
    """
    Run one governed mission for a tenant: enforce plan limits, run under the tenant's
    keys and isolated engine, then meter the realized token spend. Raises
    PermissionError when the plan limit / missing key blocks the run.
    """
    ok, reason = store.can_run(tenant)
    if not ok:
        raise PermissionError(reason)
    engine, ledger, _auth = build_tenant_engine(tenant, store)
    before = _spend_cents(ledger)
    with tenant_llm_context(tenant):
        result = await engine.run_mission_with_audit(
            goal, abort_on_audit_failure=False,
            job_revenue_cents=job_revenue_cents, max_repair_attempts=max_repair_attempts,
        )
    store.record_mission(tenant.id, spend_cents=max(0, _spend_cents(ledger) - before))
    return result
