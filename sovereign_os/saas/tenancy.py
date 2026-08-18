"""
Tenant model + store for the Sovereign-OS SaaS.

Each tenant is fully isolated: its own API key, config (bring-your-own LLM keys and,
optionally, its own Stripe/wallet — the platform never holds funds), a private data
directory (ledger, trust, audit trail, jobs), and per-day usage metered against its
plan. The store persists tenant metadata + usage as JSON; per-tenant runtime state
lives under `<root>/<tenant_id>/`.

Secrets (API keys) are stored to disk but never returned by `redacted()`, which is
what the API layer should serialize.
"""

from __future__ import annotations

import json
import secrets
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from sovereign_os.saas.plans import DEFAULT_PLAN, get_plan


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


@dataclass
class TenantConfig:
    """Tenant-supplied settings. BYO keys — never platform-custodied funds."""

    llm_provider: str = ""          # "" = infer from whichever key is set
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    stripe_api_key: str = ""        # the tenant's OWN Stripe account (they get paid directly)
    x402_pay_to: str = ""           # the tenant's OWN wallet address
    charter_yaml: str = ""          # the tenant's charter (else a default is used)
    earning_enabled: bool = False   # opt-in marketplace/earning (team plan only)

    def llm_keys(self) -> dict:
        """Keys to bind into the LLM key context for this tenant's missions."""
        return {"provider": self.llm_provider, "openai": self.openai_api_key,
                "anthropic": self.anthropic_api_key}

    def has_llm_key(self) -> bool:
        return bool(self.anthropic_api_key.strip() or self.openai_api_key.strip())

    def redacted(self) -> dict:
        """Config safe to return over the API — secrets masked, never leaked."""
        def mask(v: str) -> str:
            v = (v or "").strip()
            return (v[:6] + "…" + v[-4:]) if len(v) > 12 else ("set" if v else "")
        return {
            "llm_provider": self.llm_provider,
            "anthropic_api_key": mask(self.anthropic_api_key),
            "openai_api_key": mask(self.openai_api_key),
            "stripe_api_key": mask(self.stripe_api_key),
            "x402_pay_to": self.x402_pay_to,
            "has_charter": bool(self.charter_yaml.strip()),
            "earning_enabled": self.earning_enabled,
        }


@dataclass
class Tenant:
    id: str
    name: str
    api_key: str
    plan: str = DEFAULT_PLAN
    config: TenantConfig = field(default_factory=TenantConfig)
    created_ts: float = 0.0

    def has_feature(self, feature: str) -> bool:
        return get_plan(self.plan).allows(feature)

    def to_json(self) -> dict:
        d = asdict(self)
        return d

    @classmethod
    def from_json(cls, d: dict) -> "Tenant":
        cfg = TenantConfig(**(d.get("config") or {}))
        return cls(id=d["id"], name=d.get("name", ""), api_key=d["api_key"],
                   plan=d.get("plan", DEFAULT_PLAN), config=cfg,
                   created_ts=float(d.get("created_ts", 0.0)))

    def public(self) -> dict:
        """Serialization for the API: no api_key, redacted config."""
        return {"id": self.id, "name": self.name, "plan": self.plan,
                "created_ts": self.created_ts, "config": self.config.redacted()}


@dataclass
class TenantUsage:
    day: str = ""
    missions: int = 0
    spend_cents: int = 0


class TenantStore:
    """JSON-backed tenant registry + per-day usage meter with isolated data dirs."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._tenants_path = self.root / "tenants.json"
        self._usage_path = self.root / "usage.json"
        self._tenants: dict[str, Tenant] = {}
        self._usage: dict[str, dict] = {}
        self._load()

    # ------------------------------------------------------------------ CRUD
    def create(self, name: str, *, plan: str = DEFAULT_PLAN, config: TenantConfig | None = None) -> Tenant:
        tenant = Tenant(
            id="ten_" + uuid.uuid4().hex[:12],
            name=name.strip() or "Untitled",
            api_key="sk_ten_" + secrets.token_urlsafe(24),
            plan=get_plan(plan).name,
            config=config or TenantConfig(),
            created_ts=time.time(),
        )
        self._tenants[tenant.id] = tenant
        d = self.data_dir(tenant)
        d.mkdir(parents=True, exist_ok=True)
        # Seed the tenant's ledger with a monthly operating budget (a governance ceiling;
        # real LLM cost bills the tenant's own key). Daily limits + the circuit breaker are
        # the live guardrails. Seeded once, at signup.
        try:
            from sovereign_os.ledger.unified_ledger import UnifiedLedger

            led = UnifiedLedger(persist_path=str(d / "ledger.jsonl"))
            if led.total_usd_cents() == 0:
                led.record_usd(max(5000, get_plan(tenant.plan).max_daily_spend_cents * 30),
                               purpose="trial_credit")
        except Exception:  # noqa: BLE001 - seeding is best-effort
            pass
        self._save_tenants()
        return tenant

    def get(self, tenant_id: str) -> Tenant | None:
        return self._tenants.get(tenant_id)

    def by_api_key(self, api_key: str) -> Tenant | None:
        key = (api_key or "").strip()
        if not key:
            return None
        for t in self._tenants.values():
            if secrets.compare_digest(t.api_key, key):
                return t
        return None

    def list(self) -> list[Tenant]:
        return sorted(self._tenants.values(), key=lambda t: t.created_ts)

    def update(self, tenant: Tenant) -> None:
        tenant.plan = get_plan(tenant.plan).name
        self._tenants[tenant.id] = tenant
        self._save_tenants()

    def delete(self, tenant_id: str) -> bool:
        if tenant_id in self._tenants:
            del self._tenants[tenant_id]
            self._usage.pop(tenant_id, None)
            self._save_tenants()
            self._save_usage()
            return True
        return False

    # --------------------------------------------------------------- isolation
    def data_dir(self, tenant: Tenant | str) -> Path:
        tid = tenant.id if isinstance(tenant, Tenant) else tenant
        return self.root / tid

    # ------------------------------------------------------------------- usage
    def usage(self, tenant_id: str) -> TenantUsage:
        u = self._usage.get(tenant_id) or {}
        today = _today_utc()
        if u.get("day") != today:
            return TenantUsage(day=today, missions=0, spend_cents=0)
        return TenantUsage(day=today, missions=int(u.get("missions", 0)), spend_cents=int(u.get("spend_cents", 0)))

    def record_mission(self, tenant_id: str, *, spend_cents: int = 0) -> None:
        u = self.usage(tenant_id)
        u.missions += 1
        u.spend_cents += max(0, int(spend_cents))
        self._usage[tenant_id] = {"day": u.day, "missions": u.missions, "spend_cents": u.spend_cents}
        self._save_usage()

    def can_run(self, tenant: Tenant) -> tuple[bool, str]:
        """Plan-limit gate: daily mission count + daily spend ceiling + BYO-key present."""
        if not tenant.config.has_llm_key():
            return False, "no LLM key configured for this tenant (bring your own key)"
        plan = get_plan(tenant.plan)
        u = self.usage(tenant.id)
        if plan.max_missions_per_day and u.missions >= plan.max_missions_per_day:
            return False, f"daily mission limit reached ({plan.max_missions_per_day} on '{plan.name}')"
        if plan.max_daily_spend_cents and u.spend_cents >= plan.max_daily_spend_cents:
            return False, f"daily spend limit reached (${plan.max_daily_spend_cents/100:.2f} on '{plan.name}')"
        return True, "ok"

    # ------------------------------------------------------------- persistence
    def _load(self) -> None:
        if self._tenants_path.exists():
            try:
                data = json.loads(self._tenants_path.read_text("utf-8"))
                self._tenants = {tid: Tenant.from_json(td) for tid, td in data.items()}
            except Exception:
                self._tenants = {}
        if self._usage_path.exists():
            try:
                self._usage = json.loads(self._usage_path.read_text("utf-8"))
            except Exception:
                self._usage = {}

    def _save_tenants(self) -> None:
        self._tenants_path.write_text(
            json.dumps({tid: t.to_json() for tid, t in self._tenants.items()}), encoding="utf-8")

    def _save_usage(self) -> None:
        self._usage_path.write_text(json.dumps(self._usage), encoding="utf-8")
