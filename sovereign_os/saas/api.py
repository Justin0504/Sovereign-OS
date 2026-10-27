"""
Multi-tenant SaaS API + signup console for Sovereign-OS.

A standalone FastAPI app (separate from the single-tenant dashboard) that lets many
tenants sign up, bring their own keys, and run governed missions — each fully isolated
via TenantStore + runtime. Auth is a tenant API key (X-Tenant-Key header). The platform
never holds funds or custodies keys (see docs/SAAS.md).

Run:  python -m sovereign_os.saas.api          (serves the console at /)
Env:  SOVEREIGN_SAAS_ROOT  (tenant data root, default ".saas-data")
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import re
import hmac
import secrets as _secrets
import time
from typing import Any

from sovereign_os.saas import platform_metrics as _metrics
from sovereign_os.saas.plans import PLANS, get_plan
from sovereign_os.saas.runtime import run_tenant_mission, tenant_earning_active
from sovereign_os.saas.tenancy import TenantConfig, TenantStore

# Imported at module scope on purpose: `from __future__ import annotations` turns
# annotations into strings, and FastAPI resolves them with get_type_hints against the
# MODULE globals. A `Request` imported inside create_saas_app is invisible there, so an
# annotated parameter silently becomes a required query param and every POST 422s.
try:  # pragma: no cover - fastapi is a hard dependency; guarded for import-only use
    from fastapi import HTTPException, Request
except ImportError:  # pragma: no cover
    Request = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

_STORE: TenantStore | None = None


# A workspace name is stored, returned over the API, and rendered in a browser. The
# console no longer builds HTML from it, but validating at the boundary means a future
# surface that is less careful inherits a value that was already bounded. Control
# characters are rejected outright because their only use here is to forge log lines.
_NAME_MAX = 80
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def _clean_name(raw: Any) -> str:
    name = str(raw or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required.")
    if len(name) > _NAME_MAX:
        raise HTTPException(
            status_code=400,
            detail=f"name must be at most {_NAME_MAX} characters (got {len(name)}).")
    if _CONTROL_CHARS.search(name):
        raise HTTPException(status_code=400, detail="name contains control characters.")
    return name


def _validated_charter_yaml(raw: Any) -> str:
    """
    Parse a tenant's charter before storing it, and refuse it if it will not load.

    The Charter is the constitutional document: fiscal boundaries, competencies, KPIs.
    Previously any string was stored, the API answered 200 with has_charter=true, and the
    runtime caught the parse failure at use and quietly substituted a DEFAULT charter with
    default spending limits. A tenant who mistyped their daily cap was told it was in
    force while every mission ran under limits they never chose -- the control plane
    reporting a constraint the effect site was not applying.
    """
    text = str(raw or "")
    if not text.strip():
        return ""                      # clearing the charter is legitimate
    try:
        import yaml

        parsed = yaml.safe_load(text)
    except Exception as exc:  # noqa: BLE001 - the parser's message is the useful part
        raise HTTPException(status_code=400,
                            detail=f"charter_yaml is not valid YAML: {exc}") from None
    if not isinstance(parsed, dict):
        raise HTTPException(
            status_code=400,
            detail="charter_yaml must be a YAML mapping (got "
                   f"{type(parsed).__name__}).")
    try:
        from sovereign_os.models.charter import Charter

        Charter.model_validate(parsed)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400,
                            detail=f"charter_yaml is not a valid Charter: {exc}") from None
    return text


def _validated_provider_key(field: str, raw: Any) -> str:
    """
    Catch the paste error, not the key format.

    A deliberately loose check: provider key formats change, and a strict pattern would
    break the product the day one does. This rejects only input that cannot be a key at
    all -- a bare word, a shell variable someone forgot to expand, embedded whitespace --
    because the alternative is storing it, reporting success, and surfacing it much later
    as an opaque provider error in the middle of a mission.
    """
    text = str(raw or "").strip()
    if not text:
        return ""                      # clearing a key is legitimate
    if any(ch.isspace() for ch in text):
        raise HTTPException(status_code=400,
                            detail=f"{field} contains whitespace; check what was pasted.")
    if text.startswith("$") or text.startswith("<"):
        raise HTTPException(
            status_code=400,
            detail=f"{field} looks like an unexpanded placeholder ({text[:12]}…).")
    if not text.startswith("sk-") or len(text) < 20:
        raise HTTPException(
            status_code=400,
            detail=f"{field} does not look like a provider key (expected an 'sk-' "
                   f"prefix and at least 20 characters).")
    return text



def _effective_governance(tenant) -> dict:
    """
    The limits that will actually apply to the next mission.

    "Do I have a charter" is not the question a tenant needs answered; "what am I
    actually capped at" is. Those can differ -- a charter that fails to load used to be
    replaced silently by a default one -- so this resolves the charter the same way the
    runtime does and reports the resulting boundaries, plus whether they came from the
    tenant's own document or from the default. If the stored charter no longer loads it
    says so here rather than leaving the tenant to discover it when a mission is refused.
    """
    from sovereign_os.saas.runtime import _charter_for

    try:
        charter = _charter_for(tenant)
    except PermissionError as exc:
        return {"source": "error", "error": str(exc)}
    fb = charter.fiscal_boundaries
    return {
        "source": "charter" if tenant.config.charter_yaml.strip() else "default",
        "mission": charter.mission,
        "daily_burn_max_usd": fb.daily_burn_max_usd,
        "max_budget_usd": fb.max_budget_usd,
        "currency": fb.currency,
        "min_job_margin_ratio": fb.min_job_margin_ratio,
    }


def get_store() -> TenantStore:
    """Process-wide tenant store (created once)."""
    global _STORE
    if _STORE is None:
        _STORE = TenantStore(os.getenv("SOVEREIGN_SAAS_ROOT", ".saas-data"))
    return _STORE


_CONFIG_FIELDS = {
    "llm_provider", "anthropic_api_key", "openai_api_key",
    "stripe_api_key", "x402_pay_to", "charter_yaml", "earning_enabled",
}


def hosted_preflight(store: TenantStore) -> list[str]:
    """
    Refuse to serve other people's credentials from an unsafe configuration.

    Called at startup for a hosted deployment. Each check below is a guarantee the
    product makes in writing, and each fails silently if it is merely implemented and
    never invoked — the state where the defence exists, the tests pass, and production
    is still wide open.

    Returns the warnings that are advisory; raises on the ones that are not.
    """
    from sovereign_os.llm.providers import multi_tenant_mode

    store.require_encryption()      # raises unless SOVEREIGN_SAAS_SECRET is set

    advisories: list[str] = []
    if not multi_tenant_mode():
        advisories.append(
            "SOVEREIGN_MULTI_TENANT is not set. A tenant mission whose key context is "
            "lost — any thread boundary will do it — would silently fall back to the "
            "platform's own API key, billing the operator and breaking the "
            "bring-your-own-keys guarantee with nothing reporting a problem."
        )
    pending = store.pending_migrations()
    if pending:
        advisories.append(
            f"{len(pending)} tenant(s) still hold plaintext secrets on disk. "
            f"Run store.migrate_secrets() before serving traffic."
        )
    return advisories


def create_saas_app(store: TenantStore | None = None, *, hosted: bool = False) -> Any:
    try:
        from fastapi import Body, Depends, FastAPI, Header, HTTPException
        from fastapi.responses import HTMLResponse, PlainTextResponse
    except ImportError:  # pragma: no cover
        raise ImportError("fastapi required; pip install fastapi uvicorn")

    from sovereign_os.saas.limits import AuthThrottle, RateLimiter, client_key

    st = store or get_store()
    # Signup mints credentials and missions burn model tokens, so both need a ceiling
    # that applies BEFORE authentication — plan limits are enforced after it and do
    # nothing about a loop creating tenants or a caller probing for a key.
    signup_limiter = RateLimiter(capacity=5, refill_per_second=5 / 3600)      # ~5/hour
    mission_limiter = RateLimiter(capacity=20, refill_per_second=20 / 3600)   # ~20/hour
    auth_throttle = AuthThrottle()

    def _enforce(limiter, key: str, what: str) -> None:
        allowed, retry_after = limiter.check(key)
        if not allowed:
            raise HTTPException(
                status_code=429, detail=f"Too many {what}. Retry in {int(retry_after) + 1}s.",
                headers={"Retry-After": str(int(retry_after) + 1)},
            )

    if hosted:
        for warning in hosted_preflight(st):
            logger.warning("SAAS PREFLIGHT: %s", warning)
    app = FastAPI(title="Sovereign-OS SaaS")

    # A browser is a hostile execution environment that we hand a tenant API key to, and
    # the response had none of the headers that constrain it. The console keeps the key in
    # localStorage, so a single injected script is a key exfiltration; `frame-ancestors`
    # and `X-Frame-Options` stop the console being framed and clickjacked; `nosniff` stops
    # a JSON error body being re-interpreted as HTML.
    #
    # The CSP is nonce-based rather than `unsafe-inline`, which is why the console has no
    # `onclick=` attributes left: a nonce whitelists a <script> block, never an event
    # handler attribute, so `unsafe-inline` would have been the only alternative and that
    # whitelists the injected script too.
    @app.middleware("http")
    async def security_headers(request, call_next):
        nonce = _secrets.token_urlsafe(16)
        request.state.csp_nonce = nonce
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; "
            f"script-src 'nonce-{nonce}'; "
            f"style-src 'nonce-{nonce}' https://fonts.googleapis.com; "
            "font-src https://fonts.gstatic.com; "
            "connect-src 'self'; "
            "img-src 'self' data:; "
            "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        # Only meaningful over TLS, and a lie over plain HTTP in local dev.
        if hosted or request.url.scheme == "https":
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains")
        return response

    def require_tenant(request: Request, x_tenant_key: str | None = Header(default=None)):
        caller = client_key(request)
        locked, retry_after = auth_throttle.is_locked(caller)
        if locked:
            raise HTTPException(
                status_code=429,
                detail=f"Too many failed attempts. Retry in {int(retry_after) + 1}s.",
                headers={"Retry-After": str(int(retry_after) + 1)},
            )
        t = st.by_api_key(x_tenant_key or "")
        if t is None:
            auth_throttle.record_failure(caller)
            _metrics.incr("sovereign_auth_failures_total")
            if auth_throttle.is_locked(caller)[0]:
                _metrics.incr("sovereign_auth_lockouts_total")
            raise HTTPException(status_code=401, detail="Invalid or missing X-Tenant-Key.")
        auth_throttle.record_success(caller)
        return t

    def _plan_info(plan_name: str) -> dict:
        p = get_plan(plan_name)
        return {"name": p.name, "price_cents_month": p.price_cents_month, "seats": p.seats,
                "max_missions_per_day": p.max_missions_per_day,
                "max_daily_spend_cents": p.max_daily_spend_cents, "features": sorted(p.features)}

    @app.get("/saas/health")
    def health():
        from sovereign_os.llm.providers import multi_tenant_mode
        from sovereign_os.saas.limits import limits_are_process_local

        return {
            "status": "ok", "tenants": len(st.list()),
            "encryption": st._box is not None,
            "multi_tenant_mode": multi_tenant_mode(),
            "rate_limits_process_local": limits_are_process_local(),
            "tracked_clients": signup_limiter.tracked_keys,
        }

    @app.get("/saas/plans")
    def plans():
        return {"plans": [_plan_info(n) for n in PLANS]}

    @app.post("/saas/tenants")
    def signup(request: Request, payload: dict | None = Body(None)):
        _enforce(signup_limiter, client_key(request), "workspaces created")
        body = payload or {}
        name = _clean_name(body.get("name"))
        t = st.create(name, plan=str(body.get("plan") or "free"))
        _metrics.incr("sovereign_signups_total")
        # api_key is returned ONCE at signup; it's never returned again.
        return {"tenant": t.public(), "api_key": t.api_key}

    @app.get("/saas/tenants/me")
    def me(t=Depends(require_tenant)):
        return {"tenant": t.public(), "plan": _plan_info(t.plan),
                "usage": dataclasses.asdict(st.usage(t.id)),
                "governance": _effective_governance(t),
                "earning_active": tenant_earning_active(t)}

    @app.put("/saas/tenants/me/config")
    def update_config(payload: dict | None = Body(None), t=Depends(require_tenant)):
        try:
            return _update_config_inner(payload, t)
        except HTTPException as e:
            if e.status_code == 400:
                _metrics.incr("sovereign_config_rejected_total")
            raise

    def _update_config_inner(payload, t):
        body = payload or {}
        cfg = t.config
        # Validate everything before mutating anything: a request that half-applies and
        # then 400s leaves the tenant in a state neither of us asked for.
        staged: dict[str, Any] = {}
        for k in _CONFIG_FIELDS:
            if k not in body:
                continue
            if k == "earning_enabled":
                staged[k] = bool(body[k])
            elif k == "charter_yaml":
                staged[k] = _validated_charter_yaml(body[k])
            elif k in ("anthropic_api_key", "openai_api_key"):
                staged[k] = _validated_provider_key(k, body[k])
            else:
                staged[k] = str(body[k] or "")
        new_plan = None
        if "plan" in body:  # (billing is out of scope for the MVP; allow set for now)
            requested = str(body["plan"] or "").strip().lower()
            if requested not in PLANS:
                raise HTTPException(
                    status_code=400,
                    detail=f"unknown plan {requested!r}; choose one of "
                           f"{', '.join(sorted(PLANS))}.")
            new_plan = get_plan(requested).name

        for k, v in staged.items():
            setattr(cfg, k, v)
        if new_plan:
            t.plan = new_plan
        st.update(t)
        return {"tenant": t.public(), "plan": _plan_info(t.plan)}

    @app.post("/saas/tenants/me/missions")
    async def run_mission(request: Request, payload: dict | None = Body(None),
                          t=Depends(require_tenant)):
        _enforce(mission_limiter, t.id, "missions")     # keyed per tenant, not per IP
        goal = str((payload or {}).get("goal") or "").strip()
        if not goal:
            raise HTTPException(status_code=400, detail="goal is required.")
        try:
            repair = int((payload or {}).get("max_repair_attempts") or 0)
        except (TypeError, ValueError):
            repair = 0
        _metrics.incr("sovereign_missions_total")
        try:
            plan, results, reports = await run_tenant_mission(t, st, goal, max_repair_attempts=repair)
        except PermissionError as e:
            _metrics.incr("sovereign_missions_failed_total")
            raise HTTPException(status_code=402, detail=str(e))  # limit / no key
        except Exception as e:  # noqa: BLE001
            _metrics.incr("sovereign_missions_failed_total")
            raise HTTPException(status_code=500, detail=f"mission failed: {e}")
        all_passed = all(getattr(r, "passed", False) for r in reports) if reports else True
        _metrics.incr("sovereign_missions_passed_total" if all_passed
                      else "sovereign_missions_failed_total")
        return {
            "goal": goal,
            "all_passed": all_passed,
            "tasks": [{"task_id": r.task_id, "success": r.success, "output": (r.output or "")[:8000]}
                      for r in (results or [])],
            "audits": [{"task_id": rep.task_id, "passed": rep.passed, "score": rep.score,
                        "reason": rep.reason} for rep in (reports or [])],
            "usage": dataclasses.asdict(st.usage(t.id)),
        }

    @app.post("/saas/tenants/me/rotate-key")
    def rotate_key(request: Request, t=Depends(require_tenant)):
        """
        Issue a new workspace key and invalidate the old one.

        The workspace key is the only credential, it is stored hashed, and it is shown
        once. Before this there was no recovery from leaking it: the holder could spend
        the tenant's provider key indefinitely and the tenant could neither rotate nor
        revoke nor delete. Rate-limited on the caller, because an endpoint that mints
        credentials is worth hammering.
        """
        _enforce(signup_limiter, client_key(request), "key rotations")
        new_key = st.rotate_api_key(t)
        logger.info("SAAS: workspace key rotated for %s", t.id)
        return {"tenant": t.public(), "api_key": new_key,
                "note": "The previous key stopped working immediately. This one is "
                        "shown once."}

    @app.get("/saas/metrics")
    def metrics(request: Request):
        """
        Platform-aggregate operational metrics, Prometheus text format.

        Disabled unless SOVEREIGN_METRICS_TOKEN is set (fail closed: a hosted service does
        not expose its operational shape by default), and then requires it as a bearer
        token. Every series is a platform total -- there are no per-tenant labels, because
        a metric a scraper could break down by tenant is a cross-tenant leak through the
        monitoring port. The only live reading is the current tenant count, computed here
        rather than stored.
        """
        if not _metrics.enabled():
            raise HTTPException(status_code=404, detail="metrics are not enabled")
        auth = (request.headers.get("authorization") or "").strip()
        presented = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        if not presented or not hmac.compare_digest(presented, _metrics.metrics_token()):
            raise HTTPException(status_code=401, detail="metrics require a bearer token")
        gauges = {
            "sovereign_tenants": ("Workspaces that currently exist", float(len(st.list()))),
        }
        return PlainTextResponse(_metrics.render(gauges),
                                 media_type="text/plain; version=0.0.4")

    @app.get("/saas/demo")
    def demo(request: Request):
        """
        A governed mission a visitor can watch without supplying an API key.

        The console used to ask for an Anthropic key before showing anything, so anyone
        evaluating the system -- the entire reason a console is public -- saw a signup
        form and left. Governance does not need a model: the Treasury, TrustScore
        thresholds, audit rubric and proof hashes in this trace are the same code a paid
        mission runs through. Only the deliverable is a placeholder, and the response
        says so.

        Unauthenticated, so rate-limited on the caller.
        """
        _enforce(signup_limiter, client_key(request), "demo runs")
        from sovereign_os.saas.demo import run_demo_mission

        _metrics.incr("sovereign_demo_runs_total")
        return run_demo_mission()

    @app.get("/saas/tenants/me/export")
    def export_workspace(t=Depends(require_tenant)):
        """
        Everything this workspace holds, as JSON.

        The counterpart to deletion: being able to destroy your audit trail without being
        able to keep a copy is not control over your data, it is a choice between losing
        it and leaving it with us. Provider keys are masked here exactly as they are
        everywhere else -- this is the tenant's record of what happened, not a way to read
        back a credential we promised to keep encrypted.
        """
        d = st.data_dir(t)

        def _lines(name: str, cap: int = 50_000) -> list:
            f = d / name
            if not f.exists():
                return []
            out = []
            for line in f.read_text("utf-8", errors="ignore").splitlines()[:cap]:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    out.append({"unparsed": line})
            return out

        return {
            "tenant": t.public(),
            "plan": _plan_info(t.plan),
            "governance": _effective_governance(t),
            "usage_today": dataclasses.asdict(st.usage(t.id)),
            "charter_yaml": t.config.charter_yaml,
            "ledger": _lines("ledger.jsonl"),
            "audit_trail": _lines("audit.jsonl"),
            "exported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }

    @app.delete("/saas/tenants/me")
    def delete_workspace(payload: dict | None = Body(None), t=Depends(require_tenant)):
        """
        Delete the workspace and everything stored for it.

        Requires the workspace id in the body. A tenant handed us their provider keys;
        being able to take them back is the minimum, and a one-call irreversible wipe
        behind a header that may be sitting in someone's shell history is not something
        to offer without a deliberate confirmation.
        """
        confirm = str((payload or {}).get("confirm") or "").strip()
        if confirm != t.id:
            raise HTTPException(
                status_code=400,
                detail=f"to confirm deletion, send {{\"confirm\": \"{t.id}\"}}. "
                       f"This removes the workspace, its stored provider keys, its "
                       f"ledger and its audit trail, and cannot be undone.")
        tenant_id = t.id
        st.delete(tenant_id)
        logger.info("SAAS: workspace %s deleted at tenant request", tenant_id)
        return {"deleted": tenant_id}

    @app.get("/", response_class=HTMLResponse)
    def console(request: Request):
        nonce = getattr(request.state, "csp_nonce", "")
        return HTMLResponse(_CONSOLE_HTML.replace("__CSP_NONCE__", nonce))

    return app


# --------------------------------------------------------------------- console UI
_CONSOLE_HTML = """<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1">
<title>Sovereign-OS Console</title>
<link rel=preconnect href=https://fonts.googleapis.com>
<link rel=preconnect href=https://fonts.gstatic.com crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Hanken+Grotesk:wght@400;500;600;700&family=Instrument+Serif:ital@0;1&family=JetBrains+Mono:wght@400;500&display=swap" rel=stylesheet>
<style nonce=__CSP_NONCE__>
/* Tokens are the self-host dashboard's, deliberately. The console and the dashboard
   were visibly different products -- Inter and pure black here, Hanken Grotesk and a
   pine accent there -- which is the cheapest credibility loss a two-surface product
   can take. Three families (display / serif / mono), three neutrals, one accent. */
:root{
  --bg:hsl(40 24% 98%); --surface:#fff; --elev:hsl(40 16% 96%);
  --ink:hsl(220 14% 11%); --soft:hsl(220 8% 42%); --mut:hsl(220 7% 58%);
  --bd:hsl(40 10% 90%);
  --pine:#0c5c46; --pine-soft:#e7f2ee;
  --ok:#10a37f; --bad:#e02e2a; --warn:#b45309;
  --font-display:'Hanken Grotesk',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
  --font-serif:'Instrument Serif',Georgia,serif;
  --font-mono:'JetBrains Mono',ui-monospace,SFMono-Regular,Menlo,monospace;
  /* 1.25 ratio, so no two sizes are a near-miss of each other */
  --t-xs:0.75rem; --t-sm:0.875rem; --t-md:1rem; --t-lg:1.25rem;
  --t-xl:1.5rem; --t-2xl:2.25rem; --t-3xl:3rem;
  --ease:cubic-bezier(0.4,0,0.2,1);
}
*{box-sizing:border-box}
body{margin:0;font-family:var(--font-display);background:var(--bg);color:var(--ink);
     font-size:var(--t-md);line-height:1.6;-webkit-font-smoothing:antialiased}
.wrap{max-width:1080px;margin:0 auto;padding:64px 24px 96px}
a{color:var(--pine)}

/* ---------- masthead: answers "what is this" before any form ---------- */
.hero{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:48px;
      align-items:start;margin-bottom:64px}
.eyebrow{font-size:var(--t-xs);letter-spacing:.12em;text-transform:uppercase;
         color:var(--pine);font-weight:600;margin:0 0 12px}
h1{font-family:var(--font-serif);font-size:var(--t-3xl);line-height:1.1;
   font-weight:400;margin:0 0 16px;max-width:22ch;letter-spacing:-.01em}
h1 em{font-style:italic;color:var(--pine)}
.lede{color:var(--soft);margin:0 0 24px;max-width:46ch;font-size:var(--t-lg);line-height:1.5}
.facts{display:flex;gap:24px;flex-wrap:wrap;margin:0;padding:0;list-style:none}
.facts div{font-size:var(--t-xs);color:var(--mut)}
.facts b{display:block;font-family:var(--font-mono);font-size:var(--t-lg);
         color:var(--ink);font-weight:500;letter-spacing:-.02em}

/* ---------- the diagram: what the product actually does ---------- */
.diagram{width:100%;height:auto}
.diagram text{font-family:var(--font-mono);font-size:8px;fill:var(--soft)}
.diagram .lbl{font-family:var(--font-display);font-size:11px;font-weight:600;fill:var(--ink)}
.diagram .onDark{fill:#fff}
.diagram .onDarkMut{fill:hsl(220 7% 72%)}
.diagram .gate{fill:var(--pine-soft);stroke:var(--pine);stroke-width:1}
.diagram .step{fill:#fff;stroke:var(--bd);stroke-width:1}
.diagram .flow{stroke:var(--mut);stroke-width:1.25;fill:none}

/* ---------- cards ---------- */
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:20px}
.card{background:var(--surface);border:1px solid var(--bd);border-radius:12px;padding:24px;
      margin-bottom:20px}
.card h2{font-size:var(--t-xs);text-transform:uppercase;letter-spacing:.09em;
         color:var(--mut);margin:0 0 16px;font-weight:600}
.card h2 span{color:var(--pine)}
label{display:block;font-size:var(--t-sm);color:var(--soft);margin:16px 0 6px;font-weight:500}
input,textarea,select{width:100%;padding:12px;border:1px solid var(--bd);border-radius:8px;
  font:inherit;font-size:var(--t-sm);background:var(--surface);color:var(--ink);
  transition:border-color .15s var(--ease),box-shadow .15s var(--ease)}
input:focus,textarea:focus,select:focus{outline:none;border-color:var(--pine);
  box-shadow:0 0 0 3px var(--pine-soft)}
textarea{resize:vertical;min-height:84px;font-family:var(--font-mono);font-size:var(--t-xs);
         line-height:1.6}
.btn{padding:12px 20px;border:none;border-radius:8px;background:var(--pine);color:#fff;
  font:inherit;font-weight:600;font-size:var(--t-sm);cursor:pointer;min-height:44px;
  transition:background .15s var(--ease)}
.btn:hover{background:#0a4a39}
.btn:focus-visible{outline:2px solid var(--ink);outline-offset:2px}
.row{display:flex;gap:12px;flex-wrap:wrap;align-items:center;margin-top:16px}
.key{font-family:var(--font-mono);font-size:var(--t-xs);background:var(--elev);
  border:1px solid var(--bd);border-radius:6px;padding:12px;word-break:break-all}
.pill{display:inline-flex;gap:6px;align-items:center;font-size:var(--t-xs);
  border:1px solid var(--bd);border-radius:999px;padding:4px 12px;color:var(--soft);
  background:var(--surface);font-family:var(--font-mono)}
.dot{width:6px;height:6px;border-radius:50%;background:var(--ok)}
.msg{font-size:var(--t-sm);margin-top:12px;min-height:1em}
.msg.err{color:var(--bad)}.msg.ok{color:var(--ok)}
.hide{display:none}
.out{white-space:pre-wrap;background:var(--elev);border:1px solid var(--bd);border-radius:8px;
  padding:16px;font-family:var(--font-mono);font-size:var(--t-xs);line-height:1.6;
  margin-top:16px;max-height:360px;overflow:auto}
.mut{color:var(--mut);font-size:var(--t-xs);max-width:65ch;margin:16px 0 0}
.trace{margin-top:20px;border:1px solid var(--bd);border-radius:10px;overflow:hidden;background:var(--surface)}
.trace .r{display:flex;gap:12px;align-items:baseline;padding:10px 14px;border-top:1px solid var(--bd);font-size:var(--t-sm)}
.trace .r:first-child{border-top:none}
.trace .st{font-family:var(--font-mono);font-size:var(--t-xs);color:var(--mut);min-width:78px}
.trace .v{font-family:var(--font-mono);font-size:10px;letter-spacing:.06em;text-transform:uppercase;border-radius:999px;padding:2px 8px;white-space:nowrap}
.trace .v.allowed{background:var(--pine-soft);color:var(--pine)}
.trace .v.refused{background:#fdeceb;color:var(--bad)}
.trace .v.recorded{background:var(--elev);color:var(--soft)}
.trace .note{padding:12px 14px;background:var(--elev);color:var(--soft);font-size:var(--t-xs);line-height:1.5}
.card.danger{border-color:hsl(0 40% 88%)}
.btn.ghost{background:var(--surface);color:var(--ink);border:1px solid var(--bd)}
.btn.ghost:hover{background:var(--elev)}
.btn.warn{background:var(--bad)}.btn.warn:hover{background:#b8241f}
.steps{counter-reset:s}
@media (max-width:860px){
  .hero{grid-template-columns:1fr;gap:32px}
  h1{font-size:var(--t-2xl)}
  .wrap{padding:40px 20px 72px}
}
</style></head><body><div class=wrap>

<header class=hero>
  <div>
    <p class=eyebrow>Governed agent workspace</p>
    <h1>Agents that spend money need a <em>constitution</em>, not a prompt.</h1>
    <p class=lede>Declare the budget, the permissions and the quality bar in one
      YAML Charter. Every task is gated against it before it runs, and audited after.</p>
    <div class=row>
      <button class=btn id=btn_demo>See a governed run</button>
      <span class=mut id=demoHint>No key needed — real gates, placeholder output.</span>
    </div>
    <div id=demoOut class="trace hide"></div>
    <ul class=facts>
      <div><b>BYO</b>your keys, your spend</div>
      <div><b>0</b>funds custodied</div>
      <div><b>SHA-256</b>on every audit</div>
    </ul>
  </div>
  <svg class=diagram viewBox="0 0 420 200" role=img
       aria-label="Charter governs plan, budget check, permission, execute, audit; all flows recorded in an append-only ledger.">
    <rect x="8" y="8" width="404" height="26" rx="6" fill="hsl(220 14% 11%)"/>
    <text x="20" y="25" class="lbl onDark">CHARTER</text>
    <text x="96" y="25" class=onDarkMut>budget · permissions · KPIs</text>

    <rect class=step x="8"   y="70" width="69" height="42" rx="6"/>
    <rect class=gate x="91"  y="70" width="69" height="42" rx="6"/>
    <rect class=gate x="174" y="70" width="69" height="42" rx="6"/>
    <rect class=step x="257" y="70" width="69" height="42" rx="6"/>
    <rect class=gate x="340" y="70" width="69" height="42" rx="6"/>

    <text class=lbl x="18"  y="88">Plan</text><text x="18"  y="101">CEO</text>
    <text class=lbl x="101" y="88">Budget</text><text x="101" y="101">CFO gate</text>
    <text class=lbl x="184" y="88">Permit</text><text x="184" y="101">earned</text>
    <text class=lbl x="267" y="88">Execute</text><text x="267" y="101">worker</text>
    <text class=lbl x="350" y="88">Audit</text><text x="350" y="101">rubric</text>

    <path class=flow d="M77 91 H91 M160 91 H174 M243 91 H257 M326 91 H340"
          marker-end="url(#ar)"/>
    <defs><marker id=ar viewBox="0 0 8 8" refX="6" refY="4" markerWidth="5"
      markerHeight="5" orient="auto"><path d="M0 0 L8 4 L0 8 z" fill="hsl(220 7% 58%)"/>
    </marker></defs>

    <path class=flow d="M42 34 V70 M125 34 V70 M208 34 V70 M291 34 V70 M374 34 V70"
          stroke-dasharray="2 3"/>

    <rect x="8" y="150" width="404" height="30" rx="6" fill="hsl(40 16% 96%)"
          stroke="hsl(40 10% 90%)"/>
    <text class=lbl x="20" y="169">LEDGER</text>
    <text x="86" y="169">append-only · proof-hashed · every cent and token</text>
    <path class=flow d="M42 112 V150 M125 112 V150 M208 112 V150 M291 112 V150 M374 112 V150"
          stroke-dasharray="2 3"/>
  </svg>
</header>

<div class=grid>
  <div class=card id=signupCard>
    <h2><span>01</span> &nbsp;Create workspace</h2>
    <label for=suName>Workspace name</label><input id=suName placeholder="Acme Inc.">
    <label for=suPlan>Plan</label><select id=suPlan></select>
    <div class=row><button class=btn id=btn_signup>Create workspace</button></div>
    <div class="msg" id=suMsg></div>
    <div id=keyBox class=hide>
      <div class=mut>Save this key now — it is shown once and stored hashed, so we
        cannot show it again.</div>
      <div class=key id=apiKey></div>
    </div>
  </div>

  <div class=card>
    <h2><span>02</span> &nbsp;Sign in</h2>
    <label for=inKey>Workspace key</label><input id=inKey placeholder="sk_ten_…">
    <div class=row><button class=btn id=btn_signin>Sign in</button><span class=msg id=siMsg></span></div>
  </div>
</div>

<div id=app class=hide>
  <div class=card>
    <h2>Workspace</h2>
    <div class=row id=meRow></div>
    <div class="row hide" id=govBox></div>
  </div>
  <div class=card>
    <h2><span>03</span> &nbsp;Bring your own keys</h2>
    <label for=cfgAnthropic>Anthropic API key</label><input id=cfgAnthropic placeholder="sk-ant-…">
    <label for=cfgOpenAI>OpenAI API key</label><input id=cfgOpenAI placeholder="sk-…">
    <label for=cfgCharter>Charter YAML</label><textarea id=cfgCharter placeholder="mission: Ship weekly research briefs.&#10;fiscal_boundaries:&#10;  daily_burn_max_usd: 5.0"></textarea>
    <div class=row><button class=btn id=btn_saveConfig>Save</button><span class=msg id=cfgMsg></span></div>
    <div class=mut>Provider keys are encrypted at rest and used only for your missions.
      The platform never holds funds.</div>
  </div>
  <div class="card danger">
    <h2>Workspace control</h2>
    <p class=mut>Your workspace key is the only credential and is stored hashed, so if it
      leaks, rotating is the remedy — the old key stops working immediately.</p>
    <div class=row>
      <button class="btn ghost" id=btn_rotate>Rotate workspace key</button>
      <span class=msg id=rotMsg></span>
    </div>
    <div id=rotBox class=hide><div class=key id=rotKey></div></div>
    <p class=mut>Deleting removes this workspace, the provider keys stored in it, its
      ledger and its audit trail. It cannot be undone.</p>
    <div class=row>
      <button class="btn warn" id=btn_delete>Delete workspace</button>
      <span class=msg id=delMsg></span>
    </div>
  </div>

  <div class=card>
    <h2><span>04</span> &nbsp;Run a mission</h2>
    <label for=goal>Goal</label><textarea id=goal placeholder="Summarize this week's agent-infrastructure releases."></textarea>
    <div class=row><button class=btn id=btn_run>Run</button><span class=msg id=runMsg></span></div>
    <div id=runOut class="out hide"></div>
  </div>
</div>

<script nonce=__CSP_NONCE__>
var KEY=localStorage.getItem("sov_key")||"";
var CURRENT_ID="";
function h(){return KEY?{"X-Tenant-Key":KEY,"Content-Type":"application/json"}:{"Content-Type":"application/json"}}
function el(id){return document.getElementById(id)}
fetch("/saas/plans").then(r=>r.json()).then(d=>{
  var sel=el("suPlan");sel.textContent="";
  (d.plans||[]).forEach(function(p){
    var o=document.createElement("option");o.value=p.name;
    o.textContent=p.name+" — $"+(p.price_cents_month/100).toFixed(0)+"/mo · "+p.max_missions_per_day+" missions/day";
    sel.appendChild(o)})});
function signup(){
  el("suMsg").className="msg";el("suMsg").textContent="Creating…";
  fetch("/saas/tenants",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({name:el("suName").value,plan:el("suPlan").value})})
   .then(r=>r.json().then(d=>({ok:r.ok,d}))).then(({ok,d})=>{
     if(!ok){el("suMsg").className="msg err";el("suMsg").textContent=d.detail||"Error";return}
     el("keyBox").classList.remove("hide");el("apiKey").textContent=d.api_key;
     el("inKey").value=d.api_key;el("suMsg").className="msg ok";el("suMsg").textContent="Created. Copy your key, then Sign in.";
   }).catch(e=>{el("suMsg").className="msg err";el("suMsg").textContent=String(e)})
}
function signin(){
  KEY=el("inKey").value.trim();
  fetch("/saas/tenants/me",{headers:h()}).then(r=>r.json().then(d=>({ok:r.ok,d}))).then(({ok,d})=>{
    if(!ok){el("siMsg").className="msg err";el("siMsg").textContent=d.detail||"Invalid key";return}
    localStorage.setItem("sov_key",KEY);el("app").classList.remove("hide");renderMe(d);
  }).catch(e=>{el("siMsg").className="msg err";el("siMsg").textContent=String(e)})
}
function pill(text,dot){
  // textContent, not innerHTML: the workspace name is whatever the tenant typed at
  // signup, and the tenant API key sits in localStorage one DOM sink away.
  var s=document.createElement("span");s.className="pill";
  if(dot){var d=document.createElement("span");d.className="dot";s.appendChild(d)}
  s.appendChild(document.createTextNode(text));return s;
}
function renderMe(d){
  var c=d.tenant.config||{},u=d.usage||{},row=el("meRow");
  CURRENT_ID=d.tenant.id||"";
  // The charter is the tenant's own document; show it rather than an empty box they
  // can only overwrite blind. Only replace the editor when it is not being edited.
  var ta=el("cfgCharter");
  if(document.activeElement!==ta){ta.value=c.charter_yaml||""}
  renderGovernance(d.governance||{});
  row.textContent="";
  [[d.tenant.name,1],
   ["plan: "+d.tenant.plan,0],
   ["missions today: "+(u.missions||0)+"/"+d.plan.max_missions_per_day,0],
   ["spend today: $"+((u.spend_cents||0)/100).toFixed(2),0],
   ["anthropic: "+(c.anthropic_api_key||"—"),0],
   ["openai: "+(c.openai_api_key||"—"),0]
  ].forEach(function(p){row.appendChild(pill(p[0],p[1]))});
}
function renderGovernance(g){
  var box=el("govBox");box.textContent="";
  if(!g.source){box.classList.add("hide");return}
  box.classList.remove("hide");
  if(g.source==="error"){
    box.appendChild(pill("charter will not load — missions refused",0));
    var p=document.createElement("div");p.className="msg err";p.textContent=g.error;
    box.appendChild(p);return;
  }
  // What the next mission is actually capped at, and where the number came from.
  box.appendChild(pill("limits from: "+(g.source==="charter"?"your charter":"default"),1));
  box.appendChild(pill("daily burn cap: "+(g.daily_burn_max_usd?("$"+g.daily_burn_max_usd.toFixed(2)):"none"),0));
  box.appendChild(pill("budget ceiling: "+(g.max_budget_usd?("$"+g.max_budget_usd.toFixed(2)):"none"),0));
  box.appendChild(pill("margin floor: "+Math.round((g.min_job_margin_ratio||0)*100)+"%",0));
}
function saveConfig(){
  el("cfgMsg").className="msg";el("cfgMsg").textContent="Saving…";
  var body={};if(el("cfgAnthropic").value)body.anthropic_api_key=el("cfgAnthropic").value;
  if(el("cfgOpenAI").value)body.openai_api_key=el("cfgOpenAI").value;
  if(el("cfgCharter").value)body.charter_yaml=el("cfgCharter").value;
  fetch("/saas/tenants/me/config",{method:"PUT",headers:h(),body:JSON.stringify(body)})
   .then(r=>r.json().then(d=>({ok:r.ok,d}))).then(({ok,d})=>{
     if(!ok){el("cfgMsg").className="msg err";el("cfgMsg").textContent=d.detail||"Error";return}
     el("cfgMsg").className="msg ok";el("cfgMsg").textContent="Saved.";el("cfgAnthropic").value="";el("cfgOpenAI").value="";
     fetch("/saas/tenants/me",{headers:h()}).then(r=>r.json()).then(renderMe);
   }).catch(e=>{el("cfgMsg").className="msg err";el("cfgMsg").textContent=String(e)})
}
function run(){
  el("runMsg").className="msg";el("runMsg").textContent="Running…";el("runOut").classList.add("hide");
  fetch("/saas/tenants/me/missions",{method:"POST",headers:h(),body:JSON.stringify({goal:el("goal").value})})
   .then(r=>r.json().then(d=>({ok:r.ok,d}))).then(({ok,d})=>{
     if(!ok){el("runMsg").className="msg err";el("runMsg").textContent=d.detail||"Error";return}
     el("runMsg").className="msg ok";el("runMsg").textContent=d.all_passed?"Completed · audit passed":"Completed · audit flagged issues";
     var o=(d.tasks||[]).map(t=>"● "+t.task_id+(t.success?" ✓":" ✗")+"\\n"+(t.output||"")).join("\\n\\n");
     el("runOut").textContent=o||"(no output)";el("runOut").classList.remove("hide");
     fetch("/saas/tenants/me",{headers:h()}).then(r=>r.json()).then(renderMe);
   }).catch(e=>{el("runMsg").className="msg err";el("runMsg").textContent=String(e)})
}
function rotate(){
  if(!confirm("Rotate the workspace key? The current key stops working immediately."))return;
  el("rotMsg").className="msg";el("rotMsg").textContent="Rotating…";
  fetch("/saas/tenants/me/rotate-key",{method:"POST",headers:h()})
   .then(r=>r.json().then(d=>({ok:r.ok,d}))).then(({ok,d})=>{
     if(!ok){el("rotMsg").className="msg err";el("rotMsg").textContent=d.detail||"Error";return}
     KEY=d.api_key;localStorage.setItem("sov_key",KEY);el("inKey").value=KEY;
     el("rotKey").textContent=d.api_key;el("rotBox").classList.remove("hide");
     el("rotMsg").className="msg ok";el("rotMsg").textContent="Rotated — save the new key, it is shown once.";
   }).catch(e=>{el("rotMsg").className="msg err";el("rotMsg").textContent=String(e)})
}
function destroy(){
  var id=(CURRENT_ID||"");
  var typed=prompt("This deletes the workspace, its stored provider keys, its ledger and "+
                   "its audit trail, and cannot be undone.\n\nType the workspace id to confirm:\n"+id);
  if(!typed)return;
  el("delMsg").className="msg";el("delMsg").textContent="Deleting…";
  fetch("/saas/tenants/me",{method:"DELETE",headers:h(),body:JSON.stringify({confirm:typed.trim()})})
   .then(r=>r.json().then(d=>({ok:r.ok,d}))).then(({ok,d})=>{
     if(!ok){el("delMsg").className="msg err";el("delMsg").textContent=d.detail||"Error";return}
     localStorage.removeItem("sov_key");KEY="";
     el("app").classList.add("hide");
     el("siMsg").className="msg ok";el("siMsg").textContent="Workspace "+d.deleted+" deleted.";
   }).catch(e=>{el("delMsg").className="msg err";el("delMsg").textContent=String(e)})
}
function runDemo(){
  var btn=el("btn_demo");btn.disabled=true;el("demoHint").textContent="Running…";
  fetch("/saas/demo").then(r=>r.json()).then(function(d){
    var box=el("demoOut");box.textContent="";
    (d.steps||[]).forEach(function(s){
      var row=document.createElement("div");row.className="r";
      var st=document.createElement("span");st.className="st";st.textContent=s.stage;
      var v=document.createElement("span");v.className="v "+s.decision;v.textContent=s.decision;
      var h=document.createElement("span");h.textContent=s.headline;
      row.appendChild(st);row.appendChild(v);row.appendChild(h);box.appendChild(row);
    });
    var n=document.createElement("div");n.className="note";n.textContent=d.disclaimer||"";
    box.appendChild(n);box.classList.remove("hide");
    el("demoHint").textContent=d.refusals+" of these decisions were refusals.";
    btn.disabled=false;
  }).catch(function(e){el("demoHint").textContent=String(e);btn.disabled=false});
}
el("btn_demo").addEventListener("click",runDemo);
el("btn_rotate").addEventListener("click",rotate);
el("btn_delete").addEventListener("click",destroy);
el("btn_signup").addEventListener("click",signup);
el("btn_signin").addEventListener("click",signin);
el("btn_saveConfig").addEventListener("click",saveConfig);
el("btn_run").addEventListener("click",run);
if(KEY){el("inKey").value=KEY;signin()}
</script></div></body></html>"""


def main() -> int:  # pragma: no cover
    import uvicorn

    # Serving over a network means serving other people; preflight is not optional here.
    uvicorn.run(create_saas_app(hosted=True),
                host="0.0.0.0", port=int(os.getenv("PORT", "8020")))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
