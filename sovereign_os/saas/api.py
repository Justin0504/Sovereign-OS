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
# Raw string: the old non-raw literal turned the JS "\n" escapes into real newlines
# inside a JS string, a SyntaxError that silently killed every button on the page.
_CONSOLE_HTML = r"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1">
<title>Sovereign-OS Console</title>
<meta name=description content="Every cent your agents spend, gated. A YAML charter sets the budget, permissions and quality bar; every task is checked before it runs and audited after.">
<meta name=theme-color content="#c6daf2">
<link rel=icon href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Crect x='2' y='2' width='20' height='5' rx='1.5' fill='%23121417'/%3E%3Crect x='2' y='9.5' width='5.5' height='5' rx='1.2' fill='%23121417'/%3E%3Crect x='9.25' y='9.5' width='5.5' height='5' rx='1.2' fill='%230c5c46'/%3E%3Crect x='16.5' y='9.5' width='5.5' height='5' rx='1.2' fill='%23121417'/%3E%3Crect x='2.75' y='17.75' width='18.5' height='3.5' rx='1.2' fill='none' stroke='%23121417' stroke-width='1.5'/%3E%3C/svg%3E">
<link rel=preconnect href=https://fonts.googleapis.com>
<link rel=preconnect href=https://fonts.gstatic.com crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Hanken+Grotesk:wght@400;500;600;700&family=Instrument+Serif:ital@0;1&family=JetBrains+Mono:wght@400;500&display=swap" rel=stylesheet>
<style nonce=__CSP_NONCE__>
/* One visual world, deliberately light: a sky the product sits under, a white working
   surface floating on it, a ledger receipt as the closing object. Every colour is painted
   explicitly so the page holds on any host ground. Signed in, the marketing world steps
   aside for an app shell built from the same tokens. */
:root{
  --sky-1:#c6daf2; --sky-2:#e2ebf7; --sky-3:#f5eee2;
  --canvas:#f4f4f2; --paper:#fff; --tint:#f7f7f5; --line:#e7e7e3; --line-2:#d8d8d3;
  --ink:#121417; --ink-2:#2a2e35; --soft:#555b64; --mut:#8a9098;
  --pine:#0c5c46; --pine-2:#0a4a39; --pine-soft:#e6f1ec;
  --ok:#12805c; --ok-soft:#e5f3ec; --bad:#c8372a; --bad-soft:#fbe9e6; --amber:#a65f0b; --amber-soft:#fbf1e2;
  --sans:'Hanken Grotesk',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
  --serif:'Instrument Serif',Georgia,'Times New Roman',serif;
  --mono:'JetBrains Mono',ui-monospace,SFMono-Regular,Menlo,monospace;
  --ease:cubic-bezier(.2,.7,.2,1);
  --shadow:0 1px 2px rgba(18,20,23,.04),0 8px 24px -10px rgba(18,20,23,.12);
  --shadow-lg:0 1px 2px rgba(18,20,23,.04),0 40px 80px -40px rgba(18,20,23,.25);
}
*{box-sizing:border-box}
html{scroll-behavior:smooth;scroll-padding-top:72px}
body{margin:0;font-family:var(--sans);background:var(--canvas);color:var(--ink);
     font-size:16px;line-height:1.55;-webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}
a{color:inherit}
button{font:inherit;color:inherit}
:focus-visible{outline:2px solid var(--pine);outline-offset:2px;border-radius:4px}
.hide{display:none!important}
.mobonly{display:none!important}
.mono{font-family:var(--mono)}
.shell{max-width:1160px;margin:0 auto;padding:0 24px}
.ico{width:16px;height:16px;flex:none;stroke:currentColor;fill:none;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}

/* ---------------- nav (sticks; gains a surface once you scroll) ---------------- */
.nav{position:fixed;inset:0 0 auto 0;z-index:20;transition:background .2s,box-shadow .2s,backdrop-filter .2s}
.nav.scrolled{background:rgba(255,255,255,.78);-webkit-backdrop-filter:saturate(1.6) blur(14px);
  backdrop-filter:saturate(1.6) blur(14px);box-shadow:0 1px 0 var(--line)}
.nav .shell{display:flex;align-items:center;gap:40px;height:64px}
.brand{display:flex;align-items:center;gap:10px;text-decoration:none;font-weight:700;
       letter-spacing:.03em;font-size:14px;text-transform:uppercase}
.brand svg{width:22px;height:22px}
.links{display:flex;gap:28px;font-size:14px;color:var(--soft);flex:1}
.links a{text-decoration:none;transition:color .15s}
.links a:hover{color:var(--ink)}
.navr{display:flex;align-items:center;gap:10px;margin-left:auto}
.navr .txt{font-size:14px;color:var(--soft);text-decoration:none;padding:8px 10px}
.navr .txt:hover{color:var(--ink)}

/* ---------------- buttons ---------------- */
.btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;border:1px solid transparent;cursor:pointer;
  border-radius:8px;font-weight:600;font-size:14px;padding:9px 16px;min-height:40px;text-decoration:none;white-space:nowrap;
  transition:background .15s var(--ease),border-color .15s,transform .15s var(--ease),box-shadow .15s}
.btn:active{transform:translateY(1px)}
.btn.dark{background:var(--ink);color:#fff;box-shadow:0 1px 2px rgba(18,20,23,.2),inset 0 1px 0 rgba(255,255,255,.08)}
.btn.dark:hover{background:var(--ink-2)}
.btn.light{background:var(--paper);color:var(--ink);border-color:var(--line-2);box-shadow:0 1px 2px rgba(18,20,23,.05)}
.btn.light:hover{background:var(--tint)}
.btn.pine{background:var(--pine);color:#fff}.btn.pine:hover{background:var(--pine-2)}
.btn.warn{background:var(--paper);color:var(--bad);border-color:#ecc7c1}.btn.warn:hover{background:var(--bad-soft)}
.btn.lg{min-height:46px;padding:12px 20px;font-size:15px}
.btn[disabled]{opacity:.55;cursor:progress}

/* ---------------- sky hero ---------------- */
.sky{position:relative;overflow:hidden;padding:148px 0 230px;text-align:center;
  background:linear-gradient(180deg,var(--sky-1) 0%,var(--sky-2) 50%,var(--sky-3) 86%,var(--canvas) 100%)}
/* Clouds are two layers: a soft blurred body, and a dither of dots over it that is
   masked to the same shape -- volume first, texture second. */
.cloud{position:absolute;pointer-events:none;border-radius:50%}
.cloud::before{content:"";position:absolute;inset:0;border-radius:50%;background:#fff;filter:blur(38px);opacity:.75}
.cloud::after{content:"";position:absolute;inset:-10%;
  background-image:radial-gradient(circle,#fff 1.25px,transparent 1.7px);background-size:5px 5px;
  -webkit-mask-image:radial-gradient(closest-side,#000 25%,transparent 85%);
          mask-image:radial-gradient(closest-side,#000 25%,transparent 85%)}
.cloud.warm::before{background:#fbe3c4}
.cloud.warm::after{background-image:radial-gradient(circle,#f1c48c 1.25px,transparent 1.7px)}
.c1{width:560px;height:190px;left:-140px;top:330px}
.c2{width:480px;height:170px;right:-110px;top:270px}
.c3{width:820px;height:200px;left:20%;bottom:40px}
.c4{width:440px;height:150px;right:4%;bottom:110px}
.c5{width:380px;height:130px;left:3%;bottom:130px}
.c6{width:280px;height:90px;left:60%;top:160px;opacity:.7}
.sky .shell{position:relative;z-index:1}
.eyebrow{display:inline-flex;align-items:center;gap:16px;font-size:12px;letter-spacing:.18em;
  text-transform:uppercase;color:var(--soft);font-weight:600;margin:0 0 28px}
.eyebrow::before,.eyebrow::after{content:"";width:56px;height:1px;background:currentColor;opacity:.4}
h1{margin:0 auto;font-weight:500;font-size:clamp(3rem,8.2vw,7.2rem);line-height:.98;
   letter-spacing:-.045em;max-width:15ch;text-wrap:balance}
h1 em{font-family:var(--serif);font-style:italic;font-weight:400;letter-spacing:-.02em;font-size:1.08em}
.give{margin:38px 0 14px;font-size:14px;color:var(--soft)}
.cmd{display:flex;align-items:center;gap:8px;max-width:600px;margin:0 auto;background:var(--paper);
  border-radius:12px;padding:7px 7px 7px 16px;box-shadow:var(--shadow);text-align:left;border:1px solid rgba(18,20,23,.06)}
.cmd .p{color:var(--mut);font-family:var(--mono);font-size:13px}
.cmd code{flex:1;min-width:0;font-family:var(--mono);font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.hint{margin:14px 0 0;font-size:13px;color:var(--soft);min-height:1.4em}
.proof{display:flex;justify-content:center;gap:22px;flex-wrap:wrap;margin-top:26px;font-size:13px;color:var(--soft)}
.proof span{display:inline-flex;align-items:center;gap:7px}
.proof .ico{color:var(--pine)}

/* ---------------- bento ---------------- */
.bento{position:relative;z-index:2;margin-top:-150px;background:var(--paper);
  border:1px solid var(--line);border-radius:6px;box-shadow:var(--shadow-lg)}
.brow{display:grid;grid-template-columns:minmax(0,5fr) minmax(0,7fr);border-top:1px solid var(--line)}
.brow:first-child{border-top:none}
.brow.split{grid-template-columns:repeat(2,minmax(0,1fr))}
.brow.quad{grid-template-columns:repeat(4,minmax(0,1fr))}
.cell{padding:34px 32px;min-width:0;display:flex;flex-direction:column}
.cell+.cell{border-left:1px solid var(--line)}
.cell h3{margin:0 0 8px;font-size:21px;font-weight:600;letter-spacing:-.015em;display:flex;align-items:baseline;gap:10px;flex-wrap:wrap}
.cell h3 small{font-size:12px;font-weight:500;color:var(--mut);letter-spacing:0}
.cell p{margin:0;color:var(--soft);font-size:15px;max-width:46ch}
.vis{margin-top:auto;padding-top:24px}
.brow:first-child .cell:last-child .vis{padding-top:0}
.replaces{margin-top:18px;display:flex;gap:8px;align-items:center;flex-wrap:wrap;font-size:12px;color:var(--mut)}
.replaces span.r{border:1px solid var(--line);border-radius:6px;padding:2px 8px;color:var(--soft);
  text-decoration:line-through;text-decoration-color:rgba(200,55,42,.55)}

.yaml{background:#101317;color:#d7dce3;border-radius:10px;padding:16px 0;font-family:var(--mono);
  font-size:12.5px;line-height:1.75;overflow-x:auto;box-shadow:var(--shadow)}
.yaml .fn{display:flex;justify-content:space-between;padding:0 16px 10px;margin-bottom:8px;border-bottom:1px solid #22272e;color:#7a8390;font-size:11.5px}
.yaml .ln{display:grid;grid-template-columns:44px 1fr;white-space:pre}
.yaml .n{color:#4c535e;text-align:right;padding-right:16px;user-select:none}
.yaml .k{color:#9cc9b8}.yaml .s{color:#e8c48f}.yaml .v{color:#f0f3f6}.yaml .c{color:#6b7480}
.yaml .hl{background:rgba(156,201,184,.08);box-shadow:inset 2px 0 0 #9cc9b8}
.chips{display:flex;flex-wrap:wrap;gap:8px}
.chip{display:inline-flex;align-items:center;gap:8px;border:1px solid var(--line);border-radius:8px;
  padding:5px 10px;font-size:13px;background:var(--paper)}
.chip i{width:7px;height:7px;border-radius:50%;background:var(--ok);display:inline-block}
.chip.no i{background:var(--bad)}

.appr{background:var(--paper);border:1px solid var(--line);border-radius:12px;padding:14px 16px;box-shadow:var(--shadow);max-width:420px}
.appr+.appr{margin-top:-8px;margin-left:28px;position:relative}
.appr .top{display:flex;justify-content:space-between;font-size:12px;color:var(--mut)}
.appr .who{display:flex;gap:10px;align-items:center;font-weight:600;font-size:14px;margin-top:6px}
.badge{width:26px;height:26px;border-radius:7px;background:var(--ink);color:#fff;display:grid;place-items:center;font-size:10px;font-family:var(--mono);flex:none}
.appr .what{font-size:13px;color:var(--soft);margin:4px 0 10px 36px}
.appr .acts{display:flex;gap:8px;justify-content:flex-end}
.tag{display:inline-block;font-family:var(--mono);font-size:10.5px;letter-spacing:.05em;padding:3px 9px;border-radius:999px;text-transform:uppercase;white-space:nowrap}
.tag.allowed,.tag.pass{background:var(--ok-soft);color:var(--ok)}
.tag.refused,.tag.fail{background:var(--bad-soft);color:var(--bad)}
.tag.recorded{background:var(--tint);color:var(--soft);box-shadow:inset 0 0 0 1px var(--line)}
.meter{height:6px;border-radius:999px;background:#ecece8;position:relative;margin:8px 0 4px}
.meter b{position:absolute;left:0;top:0;bottom:0;border-radius:999px;background:var(--ink)}
.meter .m55{width:55%}.meter .m95{width:100%;background:var(--bad)}
.meter .tick{position:absolute;top:-4px;width:2px;height:14px;border-radius:1px;background:var(--bad)}
.meter .t40{left:42%}.meter .t80{left:80%}
.small{font-size:12px;color:var(--mut)}
.kv{display:flex;justify-content:space-between;font-size:13px;padding:6px 0}
.kv b{font-family:var(--mono);font-weight:500}
.mini{background:var(--tint);border:1px solid var(--line);border-radius:10px;padding:14px}
.bars{display:grid;gap:9px}
.bars div{display:grid;grid-template-columns:56px 1fr 32px;gap:10px;align-items:center;font-size:12px;color:var(--soft);font-family:var(--mono)}
.bars span.track{height:6px;background:#e7e7e2;border-radius:999px;overflow:hidden;display:block}
.bars span.fill{display:block;height:100%;background:var(--pine);border-radius:999px}
.f90{width:90%}
.hash{font-family:var(--mono);font-size:11.5px;line-height:1.6;word-break:break-all;color:var(--ink)}
.hash .dim{color:var(--mut)}
.ledgerlines{font-family:var(--mono);font-size:11.5px;line-height:2}
.ledgerlines div{display:flex;justify-content:space-between;border-bottom:1px solid var(--line)}
.ledgerlines div:last-child{border-bottom:none}
.ledgerlines .neg{color:var(--bad)}

/* ---------------- section heads ---------------- */
.band{padding:128px 0 0}
.kicker{font-size:12px;letter-spacing:.16em;text-transform:uppercase;color:var(--pine);font-weight:600;margin:0 0 14px}
h2.big{margin:0;font-weight:500;font-size:clamp(2.2rem,4.6vw,3.5rem);letter-spacing:-.035em;line-height:1.05;text-wrap:balance}
h2.big .fade{color:#b4b8be}
h2.big em{font-family:var(--serif);font-weight:400;font-style:italic;letter-spacing:-.01em}
.sub{margin:14px 0 0;color:var(--soft);font-size:17px;max-width:58ch}
.center{text-align:center}.center .sub{margin-left:auto;margin-right:auto}

/* ---------------- live run ---------------- */
.stage{position:relative;margin-top:44px;display:grid;grid-template-columns:minmax(0,1fr) 300px;align-items:start}
.win{background:var(--paper);border:1px solid var(--line);border-radius:12px;box-shadow:var(--shadow-lg);overflow:hidden}
.bar{display:flex;align-items:center;gap:8px;padding:11px 16px;border-bottom:1px solid var(--line);font-size:13.5px;font-weight:600;background:#fcfcfb}
.lights{display:flex;gap:6px;margin-right:8px}
.lights i{width:10px;height:10px;border-radius:50%;display:block;background:#e5e5e1}
.bar .sp{flex:1}
.live{display:inline-flex;align-items:center;gap:6px;font-size:12px;font-weight:500;color:var(--ok)}
.live i{width:7px;height:7px;border-radius:50%;background:var(--ok);box-shadow:0 0 0 3px var(--ok-soft)}
.stats{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));margin:16px;border:1px solid var(--line);border-radius:10px;overflow:hidden}
.stat{padding:14px 16px;min-width:0}
.stat+.stat{border-left:1px solid var(--line)}
.stat .l{font-size:12px;color:var(--mut)}
.stat .n{font-size:26px;font-weight:600;letter-spacing:-.02em;font-variant-numeric:tabular-nums;line-height:1.25}
.stat .d{font-size:12px;color:var(--soft)}
.loghead{display:flex;align-items:baseline;gap:10px;padding:2px 16px 10px;font-size:13px;flex-wrap:wrap}
.loghead b{font-weight:600}.loghead span{color:var(--mut)}
.tablewrap{overflow-x:auto}
table.log{width:100%;border-collapse:collapse;font-size:13px;min-width:620px}
table.log th{text-align:left;font-weight:500;color:var(--mut);font-size:12px;padding:8px 16px;border-top:1px solid var(--line);border-bottom:1px solid var(--line);background:#fcfcfb}
table.log td{padding:10px 16px;border-bottom:1px solid var(--line);vertical-align:top}
table.log tr:last-child td{border-bottom:none}
table.log td.stg{font-family:var(--mono);font-size:12px;color:var(--soft);white-space:nowrap}
table.log td.stg i{display:inline-block;width:6px;height:6px;border-radius:50%;background:var(--ok);margin-right:8px;vertical-align:middle}
table.log tr.refused td.stg i{background:var(--bad)}
table.log tr.recorded td.stg i{background:var(--mut)}
table.log tr.refused{background:#fdf7f5}
table.log .num{font-family:var(--mono);font-size:12px;text-align:right;white-space:nowrap;color:var(--soft);padding-right:64px}
.skel td{color:var(--mut)}
.disc{display:flex;gap:10px;padding:12px 16px;border-top:1px solid var(--line);background:var(--tint);font-size:12px;color:var(--soft);line-height:1.5}
.disc .ico{color:var(--mut);margin-top:1px}

.receipt{position:relative;margin:84px 0 0 -44px;background:#fffefb;border-radius:4px;padding:22px 22px 18px;
  box-shadow:0 30px 50px -22px rgba(18,20,23,.32),0 2px 6px rgba(18,20,23,.06);transform:rotate(3deg);z-index:2;font-size:13px}
.receipt::after{content:"";position:absolute;left:0;right:0;bottom:-8px;height:8px;
  background:radial-gradient(circle at 6px 0,transparent 5px,#fffefb 5.5px) 0 0/12px 8px repeat-x}
.receipt .hd{display:flex;justify-content:space-between;align-items:center}
.receipt .hd b{display:flex;gap:8px;align-items:center;font-size:14px}
.receipt .hd b svg{width:16px;height:16px}
.receipt .who{color:var(--mut);font-size:12px;margin:2px 0 14px}
.receipt .ln{display:flex;justify-content:space-between;gap:12px;padding:3px 0}
.receipt .ln span:first-child{color:var(--soft);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.receipt .ln span:last-child{font-family:var(--mono);font-size:12px;white-space:nowrap}
.receipt .ln.x span:last-child{color:var(--bad)}
.receipt .tot{display:flex;justify-content:space-between;align-items:center;border-top:1px dashed var(--line-2);margin-top:12px;padding-top:12px}
.receipt .tot strong{font-size:22px;letter-spacing:-.02em;font-variant-numeric:tabular-nums}
.stamp{width:62px;height:62px;border-radius:50%;border:2px solid rgba(12,92,70,.55);color:rgba(12,92,70,.8);
  display:grid;place-items:center;font-family:var(--mono);font-size:9.5px;letter-spacing:.12em;transform:rotate(-14deg);
  box-shadow:inset 0 0 0 3px #fffefb,inset 0 0 0 4px rgba(12,92,70,.35)}
.barcode{display:block;width:100%;height:38px;margin-top:16px}
.receipt .foot{text-align:center;font-size:10.5px;color:var(--mut);font-family:var(--mono);margin-top:6px;word-break:break-all}

/* ---------------- measured ---------------- */
.facts{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));margin-top:36px;border-top:1px solid var(--line-2);border-bottom:1px solid var(--line-2)}
.fact{padding:28px 20px 26px;text-align:left}
.fact+.fact{border-left:1px solid var(--line-2)}
.fact .n{font-size:44px;font-weight:500;letter-spacing:-.04em;line-height:1;font-variant-numeric:tabular-nums}
.fact .n em{font-family:var(--serif);font-weight:400;font-size:.9em}
.fact p{margin:12px 0 0;font-size:13.5px;color:var(--soft);max-width:28ch}
.src{margin-top:16px;font-size:12.5px;color:var(--mut)}
.src a{color:var(--soft)}

/* ---------------- pricing ---------------- */
.plans{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px;margin-top:40px;align-items:stretch}
.plan{position:relative;background:var(--paper);border:1px solid var(--line);border-radius:14px;padding:28px;display:flex;flex-direction:column}
.plan.hot{border-color:var(--ink);box-shadow:var(--shadow-lg)}
.plan .flag{position:absolute;top:-11px;left:28px;background:var(--ink);color:#fff;font-size:11px;font-weight:600;letter-spacing:.06em;text-transform:uppercase;padding:3px 10px;border-radius:999px}
.plan .nm{font-size:18px;font-weight:600;letter-spacing:-.01em;text-transform:capitalize}
.plan .for{font-size:13.5px;color:var(--soft);margin:4px 0 0;min-height:2.8em}
.plan .pr{font-size:46px;font-weight:500;letter-spacing:-.045em;margin:18px 0 0;font-variant-numeric:tabular-nums;line-height:1}
.plan .pr small{font-size:14px;color:var(--mut);letter-spacing:0;font-weight:400;margin-left:4px}
.plan .lim{display:grid;gap:0;margin:22px 0 0;border-top:1px solid var(--line)}
.plan .lim div{display:flex;justify-content:space-between;font-size:13.5px;padding:9px 0;border-bottom:1px solid var(--line);color:var(--soft)}
.plan .lim b{font-family:var(--mono);font-weight:500;color:var(--ink);font-size:13px}
.plan ul{list-style:none;padding:0;margin:18px 0 26px;font-size:14px;color:var(--ink-2);display:grid;gap:10px;flex:1;align-content:start}
.plan li{display:flex;gap:10px;align-items:flex-start}
.plan li .ico{color:var(--pine);margin-top:3px;width:15px;height:15px}
.plan .btn{width:100%}

/* ---------------- faq ---------------- */
.faq{display:grid;grid-template-columns:minmax(0,4fr) minmax(0,7fr);gap:56px;align-items:start}
.qa{border-top:1px solid var(--line-2)}
details{border-bottom:1px solid var(--line-2)}
summary{list-style:none;cursor:pointer;display:flex;justify-content:space-between;align-items:center;gap:16px;padding:20px 0;font-size:17px;font-weight:500;letter-spacing:-.01em}
summary::-webkit-details-marker{display:none}
summary .ico{transition:transform .2s var(--ease);color:var(--mut)}
details[open] summary .ico{transform:rotate(45deg)}
details p{margin:0 0 22px;color:var(--soft);font-size:15px;max-width:62ch}

/* ---------------- start / auth ---------------- */
.cols{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px;margin-top:36px}
.card{background:var(--paper);border:1px solid var(--line);border-radius:14px;padding:26px;min-width:0}
.card h4{margin:0 0 4px;font-size:18px;font-weight:600;letter-spacing:-.01em}
.card .lead{margin:0;font-size:14px;color:var(--soft)}
label{display:block;font-size:13px;color:var(--ink-2);margin:18px 0 6px;font-weight:500}
input,textarea,select{width:100%;padding:10px 12px;border:1px solid var(--line-2);border-radius:8px;
  font:inherit;font-size:14px;background:var(--paper);color:var(--ink);transition:border-color .15s,box-shadow .15s;
  box-shadow:0 1px 2px rgba(18,20,23,.04)}
input::placeholder,textarea::placeholder{color:#a3a8af}
input:focus,textarea:focus,select:focus{outline:none;border-color:var(--pine);box-shadow:0 0 0 3px var(--pine-soft)}
textarea{resize:vertical;min-height:110px;font-family:var(--mono);font-size:12.5px;line-height:1.65}
.row{display:flex;gap:12px;flex-wrap:wrap;align-items:center;margin-top:18px}
.key{font-family:var(--mono);font-size:12px;background:var(--tint);border:1px solid var(--line);border-radius:8px;padding:12px;word-break:break-all;margin-top:8px}
.pill{display:inline-flex;gap:6px;align-items:center;font-size:12px;border:1px solid var(--line);border-radius:999px;
  padding:4px 11px;color:var(--soft);background:var(--paper);font-family:var(--mono)}
.dot{width:6px;height:6px;border-radius:50%;background:var(--ok)}
.msg{font-size:13px;min-height:1em}
.msg.err{color:var(--bad)}.msg.ok{color:var(--ok)}
.mut{color:var(--mut);font-size:12.5px;max-width:65ch;margin:12px 0 0}

/* ---------------- closing CTA + footer ---------------- */
.closer{margin:128px 0 0;background:var(--ink);color:#fff;border-radius:18px;padding:64px 56px;position:relative;overflow:hidden;
  display:grid;grid-template-columns:minmax(0,1fr) auto;gap:32px;align-items:end}
.closer::after{content:"";position:absolute;inset:0;pointer-events:none;
  background-image:radial-gradient(circle,rgba(255,255,255,.09) 1px,transparent 1.4px);background-size:6px 6px;
  -webkit-mask-image:linear-gradient(110deg,transparent 45%,#000);mask-image:linear-gradient(110deg,transparent 45%,#000)}
.closer h2{margin:0;font-size:clamp(2rem,4vw,3.1rem);font-weight:500;letter-spacing:-.035em;line-height:1.05;max-width:18ch}
.closer h2 em{font-family:var(--serif);font-weight:400;font-style:italic}
.closer p{margin:14px 0 0;color:#aab0b8;max-width:50ch}
.closer .acts{display:flex;gap:10px;position:relative;z-index:1;flex-wrap:wrap}
.closer .btn.light{background:#fff;border-color:#fff}
.closer .btn.ghost{background:transparent;color:#fff;border-color:#3a3f47}
.closer .btn.ghost:hover{background:#1d2026}
footer{margin-top:72px;border-top:1px solid var(--line-2);padding:44px 0 48px;font-size:13.5px;color:var(--soft)}
.fgrid{display:grid;grid-template-columns:minmax(0,2fr) repeat(3,minmax(0,1fr));gap:32px}
.fgrid h5{margin:0 0 12px;font-size:12px;letter-spacing:.12em;text-transform:uppercase;color:var(--mut);font-weight:600}
.fgrid a{display:block;text-decoration:none;padding:3px 0}
.fgrid a:hover{color:var(--ink)}
.fgrid a.brand{display:flex;color:var(--ink)}
.fgrid p{margin:12px 0 0;max-width:34ch;color:var(--mut)}
.legal{margin-top:40px;display:flex;justify-content:space-between;gap:16px;flex-wrap:wrap;color:var(--mut);font-size:12.5px}

/* ================= signed-in app shell ================= */
body.authed #mkt{display:none}
body.authed .nav{display:none}
#app{min-height:100vh;display:grid;grid-template-columns:240px minmax(0,1fr)}
.side{background:#fbfbfa;border-right:1px solid var(--line);padding:18px 14px;position:sticky;top:0;height:100vh;display:flex;flex-direction:column;gap:4px}
.side .brand{padding:6px 8px 18px}
.ws{display:flex;align-items:center;gap:10px;padding:10px;border:1px solid var(--line);border-radius:10px;background:var(--paper);margin-bottom:14px}
.ws .av{width:30px;height:30px;border-radius:8px;background:var(--pine);color:#fff;display:grid;place-items:center;font-weight:600;font-size:13px;flex:none}
.ws .nm{font-size:13.5px;font-weight:600;line-height:1.2;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ws .pl{font-size:11.5px;color:var(--mut);text-transform:capitalize}
.ws>div{min-width:0}
.tab{display:flex;align-items:center;gap:10px;width:100%;padding:8px 10px;border-radius:8px;border:none;background:none;cursor:pointer;
  font-size:14px;color:var(--soft);text-align:left}
.tab:hover{background:#f0f0ed;color:var(--ink)}
.tab[aria-selected=true]{background:var(--paper);color:var(--ink);font-weight:600;box-shadow:0 0 0 1px var(--line),0 1px 2px rgba(18,20,23,.05)}
.side .grow{flex:1}
.side .foot{font-size:12px;color:var(--mut);padding:8px 10px;display:grid;gap:4px}
.side .foot a{text-decoration:none}.side .foot a:hover{color:var(--ink)}
.main{min-width:0;padding:28px 40px 64px}
.topbar{display:flex;align-items:center;justify-content:space-between;gap:16px;padding-bottom:22px;border-bottom:1px solid var(--line);margin-bottom:28px}
.topbar h1{font-size:24px;letter-spacing:-.02em;margin:0;max-width:none;line-height:1.2;text-align:left}
.topbar p{margin:2px 0 0;font-size:13.5px;color:var(--soft)}
.panel{display:grid;gap:16px}
.tiles{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:16px}
.tile{background:var(--paper);border:1px solid var(--line);border-radius:12px;padding:16px 18px;min-width:0}
.tile .l{font-size:12.5px;color:var(--soft);display:flex;justify-content:space-between;gap:8px}
.tile .n{font-size:28px;font-weight:600;letter-spacing:-.025em;margin-top:6px;font-variant-numeric:tabular-nums;line-height:1.15}
.tile .n small{font-size:14px;color:var(--mut);font-weight:500;letter-spacing:0}
.tile .meter{margin:12px 0 2px}
.tile .meter b{background:var(--pine)}
.tile .d{font-size:12px;color:var(--mut);margin-top:6px}
.two{display:grid;grid-template-columns:minmax(0,3fr) minmax(0,2fr);gap:16px}
.card .hdr{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:6px}
.empty{border:1px dashed var(--line-2);border-radius:10px;padding:22px;text-align:center;color:var(--soft);font-size:14px;margin-top:16px}
.results{display:grid;gap:10px;margin-top:16px}
.res{border:1px solid var(--line);border-radius:10px;overflow:hidden}
.res .rh{display:flex;align-items:center;gap:10px;padding:10px 14px;background:#fcfcfb;border-bottom:1px solid var(--line);font-size:13px}
.res .rh .id{font-family:var(--mono);font-weight:500}
.res .rh .sp{flex:1}
.res .sc{font-family:var(--mono);font-size:12px;color:var(--soft)}
.res .why{padding:10px 14px 0;font-size:13px;color:var(--soft)}
.res pre{margin:0;padding:12px 14px 14px;white-space:pre-wrap;font-family:var(--mono);font-size:12px;line-height:1.6;max-height:280px;overflow:auto}
.sum{display:flex;align-items:center;gap:10px;margin-top:18px;font-size:14px;font-weight:500}
.gov{display:grid;gap:0}
.gov div{display:flex;justify-content:space-between;gap:12px;padding:11px 0;border-bottom:1px solid var(--line);font-size:14px;color:var(--soft)}
.gov div:last-child{border-bottom:none}
.gov b{font-family:var(--mono);font-weight:500;color:var(--ink);font-size:13px}
.danger{border-color:#efd3ce}
.dz{display:flex;justify-content:space-between;align-items:center;gap:24px;padding:18px 0;border-top:1px solid var(--line)}
.dz:first-of-type{border-top:none}
.dz h5{margin:0;font-size:14.5px;font-weight:600}
.dz p{margin:4px 0 0;font-size:13px;color:var(--soft);max-width:56ch}

@keyframes rise{from{opacity:.001;transform:translateY(10px)}to{opacity:1;transform:none}}
.sky h1,.sky .give,.sky .cmd,.sky .proof{animation:rise .8s var(--ease) both}
.sky .give{animation-delay:.1s}.sky .cmd{animation-delay:.16s}.sky .proof{animation-delay:.24s}
table.log tbody tr{animation:rise .4s var(--ease) both}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}html{scroll-behavior:auto}}

@media (max-width:980px){
  .links,.navr .txt{display:none}
  .brow,.brow.split{grid-template-columns:minmax(0,1fr)}
  .brow.quad{grid-template-columns:repeat(2,minmax(0,1fr))}
  .cell+.cell{border-left:none;border-top:1px solid var(--line)}
  .brow.quad .cell:nth-child(2n){border-left:1px solid var(--line)}
  .brow.quad .cell:nth-child(2){border-top:none}
  .brow:first-child .cell:last-child .vis{padding-top:0}
  .stage{grid-template-columns:minmax(0,1fr)}
  .receipt{margin:-20px auto 0;max-width:320px;transform:rotate(2deg)}
  table.log .num{padding-right:16px}
  .facts{grid-template-columns:repeat(2,minmax(0,1fr))}
  .fact:nth-child(3){border-left:none}
  .fact:nth-child(n+3){border-top:1px solid var(--line-2)}
  .plans,.cols,.faq,.two{grid-template-columns:minmax(0,1fr)}
  .faq{gap:20px}
  .closer{grid-template-columns:minmax(0,1fr);padding:44px 28px}
  .fgrid{grid-template-columns:repeat(2,minmax(0,1fr))}
  #app{grid-template-columns:minmax(0,1fr)}
  .side{position:static;height:auto;flex-direction:row;flex-wrap:wrap;align-items:center;border-right:none;border-bottom:1px solid var(--line);padding:12px 16px}
  .side .brand,.side .grow,.side .foot{display:none}
  .ws{margin:0 0 8px;flex:1 1 100%}
  #tabs{display:flex;gap:4px;overflow-x:auto;flex:1 1 100%;scrollbar-width:none}
  .tab{width:auto;flex:none}
  #btn_signout{display:none}
  .mobonly{display:inline-flex!important}
  .main{padding:22px 16px 48px}
  .tiles{grid-template-columns:repeat(2,minmax(0,1fr))}
}
@media (max-width:600px){
  .shell{padding:0 16px}
  .sky{padding:116px 0 180px}
  .eyebrow{gap:10px;letter-spacing:.14em}.eyebrow::before,.eyebrow::after{width:22px}
  .brow.quad{grid-template-columns:minmax(0,1fr)}
  .brow.quad .cell:nth-child(2n){border-left:none}
  .brow.quad .cell:nth-child(2){border-top:1px solid var(--line)}
  .cell{padding:28px 20px}
  .cmd{flex-wrap:wrap}.cmd .p{display:none}.cmd code{flex-basis:100%;font-size:11.5px;padding:6px 0}
  .stat .n{font-size:20px}
  .fact .n{font-size:34px}
  .topbar{flex-wrap:wrap}
  .dz{flex-direction:column;align-items:flex-start}
}
</style></head><body>

<div id=mkt>
<header class=nav id=nav><div class=shell>
  <a class=brand href="#top" aria-label="Sovereign-OS home">
    <svg viewBox="0 0 24 24" aria-hidden=true><rect x="2" y="2" width="20" height="5" rx="1.5" fill="#121417"/><rect x="2" y="9.5" width="5.5" height="5" rx="1.2" fill="#121417"/><rect x="9.25" y="9.5" width="5.5" height="5" rx="1.2" fill="#0c5c46"/><rect x="16.5" y="9.5" width="5.5" height="5" rx="1.2" fill="#121417"/><rect x="2.75" y="17.75" width="18.5" height="3.5" rx="1.2" fill="none" stroke="#121417" stroke-width="1.5"/></svg>
    Sovereign-OS
  </a>
  <nav class=links aria-label=Primary>
    <a href="#product">Product</a><a href="#live">Live run</a><a href="#pricing">Pricing</a><a href="#faq">FAQ</a>
    <a href="https://arxiv.org/abs/2603.14011">Paper</a>
  </nav>
  <div class=navr>
    <a class=txt href="https://github.com/Justin0504/Sovereign-OS">GitHub</a>
    <a class=txt href="#signin">Sign in</a>
    <a class="btn dark" href="#start">Get started</a>
  </div>
</div></header>

<main id=top>
<section class=sky>
  <div class="cloud c1"></div><div class="cloud c2"></div><div class="cloud warm c3"></div>
  <div class="cloud warm c4"></div><div class="cloud c5"></div><div class="cloud c6"></div>
  <div class=shell>
    <p class=eyebrow>The governed agent runtime</p>
    <h1><em>Every</em> cent your agents spend, gated.</h1>
    <p class=give>Run a governed mission right now &mdash; no key, no signup</p>
    <div class=cmd>
      <span class=p aria-hidden=true>$</span>
      <code id=cmdText>curl -s https://sovereign-saas-production.up.railway.app/saas/demo</code>
      <button class="btn light" id=btn_copy type=button>Copy</button>
      <button class="btn dark" id=btn_demo type=button>Run it</button>
    </div>
    <p class=hint id=demoHint>Real charter, real CFO gate, real proof hashes. The run appears below.</p>
    <div class=proof>
      <span><svg class=ico viewBox="0 0 24 24"><path d="M20 6 9 17l-5-5"/></svg>MIT licensed</span>
      <span><svg class=ico viewBox="0 0 24 24"><path d="M20 6 9 17l-5-5"/></svg>Self-host or hosted</span>
      <span><svg class=ico viewBox="0 0 24 24"><path d="M20 6 9 17l-5-5"/></svg>Your model keys, never our funds</span>
    </div>
  </div>
</section>

<div class=shell id=product>
  <div class=bento>
    <div class=brow>
      <div class=cell>
        <h3>Charter</h3>
        <p>One YAML file is the constitution: mission, budget, per-task ceiling, quality bar.
          It is checked in with your code and enforced on every task, not suggested in a prompt.</p>
        <div class=replaces>Replaces <span class=r>"please be careful with spend"</span> <span class=r>cost spreadsheets</span></div>
      </div>
      <div class=cell>
        <div class=vis>
        <div class=yaml role=img aria-label="Example charter: daily burn max 2 dollars, budget 25 dollars, per-task ceiling 40 cents">
          <div class=fn><span>charter.yaml</span><span>enforced &middot; v1</span></div>
          <div class=ln><span class=n>1</span><span><span class=k>mission</span>: <span class=s>"Deliver research briefs and outreach copy."</span></span></div>
          <div class=ln><span class=n>2</span><span><span class=k>core_competencies</span>:</span></div>
          <div class=ln><span class=n>3</span><span>  - <span class=k>name</span>: <span class=v>research</span>     <span class=k>priority</span>: <span class=v>8</span></span></div>
          <div class=ln><span class=n>4</span><span>  - <span class=k>name</span>: <span class=v>write_email</span>  <span class=k>priority</span>: <span class=v>7</span></span></div>
          <div class=ln><span class=n>5</span><span><span class=k>fiscal_boundaries</span>:</span></div>
          <div class="ln hl"><span class=n>6</span><span>  <span class=k>daily_burn_max_usd</span>: <span class=v>2.00</span></span></div>
          <div class=ln><span class=n>7</span><span>  <span class=k>max_budget_usd</span>: <span class=v>25.00</span></span></div>
          <div class="ln hl"><span class=n>8</span><span>  <span class=k>max_task_cost_usd</span>: <span class=v>0.40</span>   <span class=c># the ceiling task 3 hits</span></span></div>
          <div class=ln><span class=n>9</span><span>  <span class=k>currency</span>: <span class=v>USD</span></span></div>
        </div>
        </div>
      </div>
    </div>

    <div class="brow split">
      <div class=cell>
        <h3>CFO gate <small>before anything runs</small></h3>
        <p>Every task is priced against the charter first. Over the ceiling, it never starts &mdash;
          no tokens burned, no half-finished work to clean up.</p>
        <div class=vis>
          <div class=appr>
            <div class=top><span>Treasury</span><span class=mono>task-3-research</span></div>
            <div class=who><span class=badge>CFO</span>Commission a full 40-page market report</div>
            <div class=what>Estimated 95&cent; &middot; per-task ceiling 40&cent;</div>
            <div class=meter><b class=m95></b><span class="tick t40"></span></div>
            <div class=acts><span class="tag refused">Refused</span></div>
          </div>
          <div class=appr>
            <div class=top><span>Treasury</span><span class=mono>task-1-research</span></div>
            <div class=who><span class=badge>CFO</span>Survey three competing governance tools</div>
            <div class=what>Estimated 18&cent; &middot; per-task ceiling 40&cent;</div>
            <div class=acts><span class="tag allowed">Approved</span></div>
          </div>
        </div>
      </div>
      <div class=cell>
        <h3>Earned permissions</h3>
        <p>Capabilities unlock with a TrustScore the worker earns from passed audits. Reading
          files is cheap to grant; spending money is not.</p>
        <div class=vis><div class=mini>
          <div class=kv><span>worker-demo &middot; TrustScore</span><b>55</b></div>
          <div class=meter><b class=m55></b><span class="tick t80"></span></div>
          <div class="kv small"><span>0</span><span>spend_usd unlocks at 80</span></div>
          <div class=chips>
            <span class=chip><i></i>read_files &middot; needs 10</span>
            <span class="chip no"><i></i>spend_usd &middot; needs 80</span>
          </div>
        </div></div>
      </div>
    </div>

    <div class="brow quad">
      <div class=cell>
        <h3>Audit</h3>
        <p>A judge scores each deliverable against a rubric. The deliverable is fenced as data, never instructions.</p>
        <div class=vis><div class="mini bars" aria-label="Judge scores from the demo run">
          <div>task-1<span class=track><span class="fill f90"></span></span><span>0.90</span></div>
          <div>task-2<span class=track><span class="fill f90"></span></span><span>0.90</span></div>
          <div>task-3<span class=track></span><span>&mdash;</span></div>
        </div></div>
      </div>
      <div class=cell>
        <h3>Proof hash</h3>
        <p>Every audit is sealed with SHA-256 over the task, output and verdict. Change a byte, the hash breaks.</p>
        <div class=vis><div class="mini hash" id=hashBox><span class=dim>sha256</span> 4c8dac89494a5529e29230e9fa415124b1935cd6e385a7fb4c727ac070cd5669</div></div>
      </div>
      <div class=cell>
        <h3>Ledger</h3>
        <p>Append-only. Every cent and token, attributed at the model client, not self-reported.</p>
        <div class=vis><div class="mini ledgerlines">
          <div><span>budget</span><span>+$25.00</span></div>
          <div><span>task-1 tokens</span><span class=neg>&minus;$0.18</span></div>
          <div><span>task-2 tokens</span><span class=neg>&minus;$0.12</span></div>
          <div><span>task-3</span><span>refused</span></div>
        </div></div>
      </div>
      <div class=cell>
        <h3>Your keys</h3>
        <p>Bring your own Anthropic or OpenAI key. Encrypted at rest, used only for your missions.</p>
        <div class=vis><div class=chips>
          <span class=chip><i></i>Anthropic</span><span class=chip><i></i>OpenAI</span>
          <span class=chip><i></i>0 funds custodied</span>
        </div></div>
      </div>
    </div>
  </div>
</div>

<section class=band id=live><div class=shell>
  <p class=kicker>Live</p>
  <h2 class=big>One charter, one ledger, <span class=fade>every decision.</span></h2>
  <p class=sub>A real run against the hosted control plane, executed when you opened this page.
    Two of its decisions are refusals &mdash; that is the product working.</p>
  <div class=stage>
    <div class=win>
      <div class=bar><span class=lights aria-hidden=true><i></i><i></i><i></i></span>Activity<span class=sp></span>
        <span class=live id=runTag><i></i>demo mission</span></div>
      <div class=stats>
        <div class=stat><div class=l>Decisions</div><div class=n id=stDecisions>&ndash;</div><div class=d>gated against the charter</div></div>
        <div class=stat><div class=l>Refused</div><div class=n id=stRefused>&ndash;</div><div class=d>before any token was spent</div></div>
        <div class=stat><div class=l>Spend</div><div class=n id=stSpend>&ndash;</div><div class=d id=stSpendD>of the daily cap</div></div>
      </div>
      <div class=loghead><b>Decision log</b><span id=logCount>loading the live run&hellip;</span></div>
      <div class=tablewrap>
        <table class=log><thead><tr><th>Stage</th><th>Decision</th><th>What happened</th><th class=num>Amount</th></tr></thead>
          <tbody id=demoOut><tr class=skel><td class=stg>&mdash;</td><td></td><td>Fetching the run&hellip;</td><td></td></tr></tbody></table>
      </div>
      <div class=disc><svg class=ico viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 8v5M12 16h.01"/></svg><span id=demoDisc>Governance decisions are real; no model is called in the demo, so task output is a placeholder.</span></div>
    </div>
    <aside class=receipt aria-label="Ledger receipt for the live run">
      <div class=hd><b><svg viewBox="0 0 24 24" aria-hidden=true><rect x="2" y="2" width="20" height="5" rx="1.5" fill="#121417"/><rect x="9.25" y="9.5" width="5.5" height="5" rx="1.2" fill="#0c5c46"/><rect x="2.75" y="17.75" width="18.5" height="3.5" rx="1.2" fill="none" stroke="#121417" stroke-width="1.5"/></svg>Ledger</b><span class=small id=rcDate></span></div>
      <div class=who>Demo workspace &middot; charter v1</div>
      <div id=rcLines><div class=ln><span>Waiting for the run</span><span>&hellip;</span></div></div>
      <div class=tot><div><div class=small>Total spent</div><strong id=rcTotal>&ndash;</strong><div class=small id=rcBal></div></div><div class=stamp>AUDITED</div></div>
      <svg class=barcode id=rcBarcode viewBox="0 0 256 38" preserveAspectRatio=none aria-hidden=true></svg>
      <div class=foot id=rcFoot>proof hash</div>
    </aside>
  </div>
</div></section>

<section class=band><div class=shell>
  <p class=kicker>Measured, not claimed</p>
  <h2 class=big>We tested it <em>against itself</em>.</h2>
  <div class=facts>
    <div class=fact><div class=n>49</div><p>real model runs in the cost-calibration study</p></div>
    <div class=fact><div class=n>94<em>%</em></div><p>of pre-flight cost estimates land within 2&times; of the metered spend</p></div>
    <div class=fact><div class=n>14</div><p>threat-model attacks exercised against the running system</p></div>
    <div class=fact><div class=n>5</div><p>of them were real holes &mdash; found, fixed, and pinned by tests</p></div>
  </div>
  <p class=src>From the paper, <a href="https://arxiv.org/abs/2603.14011">arXiv 2603.14011</a>, which also reports where our own evaluation caught us overclaiming.</p>
</div></section>

<section class=band id=pricing><div class=shell>
  <div class=center>
    <p class=kicker>Pricing</p>
    <h2 class=big>Start free. <span class=fade>Pay when it earns.</span></h2>
    <p class=sub>Model spend runs on your own key, so a plan buys governance capacity &mdash; never tokens at a markup.</p>
  </div>
  <div class=plans id=plans></div>
</div></section>

<section class=band id=faq><div class=shell>
  <div class=faq>
    <div><p class=kicker>FAQ</p><h2 class=big>Questions a CFO would ask.</h2></div>
    <div class=qa>
      <details open><summary>Do you hold my money?<svg class=ico viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg></summary>
        <p>No. Model calls are billed to your own Anthropic or OpenAI key. The platform stores those keys encrypted, uses them only for your missions, and never custodies funds.</p></details>
      <details><summary>What happens when a task would exceed the budget?<svg class=ico viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg></summary>
        <p>The CFO gate prices every task before it runs. Over the per-task ceiling or the daily burn cap, the task is refused and recorded in the ledger &mdash; no tokens are spent on it. A circuit breaker also stops a mission whose metered spend runs away mid-flight.</p></details>
      <details><summary>Is the live run on this page real?<svg class=ico viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg></summary>
        <p>The governance is: the Treasury, TrustScore thresholds, audit rubric and SHA-256 proof hashes are the same code a paid mission runs through. No model is called in the demo, so its task output is a placeholder.</p></details>
      <details><summary>How accurate are the cost estimates?<svg class=ico viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg></summary>
        <p>Across 49 real model runs, 94% of pre-flight estimates landed within 2&times; of the metered spend. The ledger always records the metered number, measured at the model client rather than reported by the agent.</p></details>
      <details><summary>Can I self-host?<svg class=ico viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg></summary>
        <p>Yes. Sovereign-OS is MIT licensed and runs on your own machine with the same charter format. The hosted edition adds isolated multi-tenant workspaces, key rotation and export.</p></details>
    </div>
  </div>
</div></section>

<section class=band id=start><div class=shell>
  <p class=kicker>Get started</p>
  <h2 class=big>Open a <em>workspace</em>.</h2>
  <p class=sub>Your key is shown once and stored hashed. Lose it and you rotate; we cannot read it back.</p>
  <div class=cols>
    <div class=card id=signupCard>
      <h4>Create workspace</h4><p class=lead>Free plan, no card.</p>
      <label for=suName>Workspace name</label><input id=suName placeholder="Acme Inc." autocomplete=organization maxlength=80>
      <label for=suPlan>Plan</label><select id=suPlan></select>
      <div class=row><button class="btn dark" id=btn_signup type=button>Create workspace</button><span class=msg id=suMsg></span></div>
      <div id=keyBox class=hide>
        <p class=mut>Save this key now &mdash; it is shown once.</p>
        <div class=key id=apiKey></div>
      </div>
    </div>
    <div class=card id=signin>
      <h4>Sign in</h4><p class=lead>Paste your workspace key.</p>
      <label for=inKey>Workspace key</label><input id=inKey placeholder="sk_ten_&hellip;" autocomplete=off spellcheck=false>
      <div class=row><button class="btn dark" id=btn_signin type=button>Sign in</button><span class=msg id=siMsg></span></div>
    </div>
  </div>
</div></section>

<div class=shell>
  <div class=closer>
    <div><h2>Put a <em>charter</em> between your agents and your card.</h2>
      <p>Free to start. Runs on your own model keys. Every decision on the record.</p></div>
    <div class=acts><a class="btn light lg" href="#start">Create a workspace</a><a class="btn ghost lg" href="https://github.com/Justin0504/Sovereign-OS">Self-host on GitHub</a></div>
  </div>
</div>
</main>

<footer><div class=shell>
  <div class=fgrid>
    <div><a class=brand href="#top"><svg viewBox="0 0 24 24" aria-hidden=true><rect x="2" y="2" width="20" height="5" rx="1.5" fill="#121417"/><rect x="2" y="9.5" width="5.5" height="5" rx="1.2" fill="#121417"/><rect x="9.25" y="9.5" width="5.5" height="5" rx="1.2" fill="#0c5c46"/><rect x="16.5" y="9.5" width="5.5" height="5" rx="1.2" fill="#121417"/><rect x="2.75" y="17.75" width="18.5" height="3.5" rx="1.2" fill="none" stroke="#121417" stroke-width="1.5"/></svg>Sovereign-OS</a>
      <p>A charter-governed operating system for agents that spend money.</p></div>
    <div><h5>Product</h5><a href="#product">Primitives</a><a href="#live">Live run</a><a href="#pricing">Pricing</a><a href="#faq">FAQ</a></div>
    <div><h5>Research</h5><a href="https://arxiv.org/abs/2603.14011">Paper (arXiv)</a><a href="https://sovereign-os-691.pages.dev">Measurements</a></div>
    <div><h5>Open source</h5><a href="https://github.com/Justin0504/Sovereign-OS">GitHub</a><a href="https://github.com/Justin0504/Sovereign-OS/blob/main/LICENSE">MIT License</a></div>
  </div>
  <div class=legal><span>&copy; 2026 Sovereign-OS</span><span>The platform never holds funds.</span></div>
</div></footer>
</div>

<!-- ================= signed-in app ================= -->
<div id=app class=hide>
  <aside class=side>
    <a class=brand href="#" id=appBrand><svg viewBox="0 0 24 24" aria-hidden=true><rect x="2" y="2" width="20" height="5" rx="1.5" fill="#121417"/><rect x="2" y="9.5" width="5.5" height="5" rx="1.2" fill="#121417"/><rect x="9.25" y="9.5" width="5.5" height="5" rx="1.2" fill="#0c5c46"/><rect x="16.5" y="9.5" width="5.5" height="5" rx="1.2" fill="#121417"/><rect x="2.75" y="17.75" width="18.5" height="3.5" rx="1.2" fill="none" stroke="#121417" stroke-width="1.5"/></svg>Sovereign-OS</a>
    <div class=ws><div class=av id=wsAv>W</div><div><div class=nm id=wsName>Workspace</div><div class=pl id=wsPlan>free plan</div></div></div>
    <div role=tablist aria-label="Workspace sections" id=tabs>
      <button class=tab role=tab aria-selected=true data-p=overview type=button><svg class=ico viewBox="0 0 24 24"><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/></svg>Overview</button>
      <button class=tab role=tab aria-selected=false data-p=run type=button><svg class=ico viewBox="0 0 24 24"><path d="M6 4l14 8-14 8z"/></svg>Run a mission</button>
      <button class=tab role=tab aria-selected=false data-p=config type=button><svg class=ico viewBox="0 0 24 24"><path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><path d="M14 3v6h6M8 13h8M8 17h5"/></svg>Charter &amp; keys</button>
      <button class=tab role=tab aria-selected=false data-p=settings type=button><svg class=ico viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/></svg>Settings</button>
    </div>
    <div class=grow></div>
    <div class=foot><a href="https://arxiv.org/abs/2603.14011">Paper</a><a href="https://github.com/Justin0504/Sovereign-OS">GitHub</a></div>
    <button class=tab id=btn_signout type=button><svg class=ico viewBox="0 0 24 24"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9"/></svg>Sign out</button>
  </aside>

  <section class=main>
    <div class=topbar><div><h1 id=pTitle>Overview</h1><p id=pSub>Today's usage against your plan and your charter.</p></div>
      <div class=row><div class=row id=meRow></div><button class="btn light mobonly" id=btn_signout2 type=button>Sign out</button></div></div>

    <div class=panel data-panel=overview>
      <div class=tiles>
        <div class=tile><div class=l><span>Missions today</span><span id=tMisPct></span></div><div class=n id=tMis>0</div><div class=meter><b id=mMis></b></div><div class=d id=tMisD></div></div>
        <div class=tile><div class=l><span>Spend today</span><span id=tSpPct></span></div><div class=n id=tSp>$0.00</div><div class=meter><b id=mSp></b></div><div class=d id=tSpD></div></div>
        <div class=tile><div class=l><span>Daily burn cap</span></div><div class=n id=tCap>&ndash;</div><div class=d id=tCapD>from your charter</div></div>
        <div class=tile><div class=l><span>Model keys</span></div><div class=n id=tKeys>0<small> / 2</small></div><div class=d id=tKeysD>configured</div></div>
      </div>
      <div class=two>
        <div class=card><div class=hdr><h4>Next step</h4></div><p class=lead id=nextLead>Add a model key and a charter, then run your first mission.</p>
          <div class=row><button class="btn dark" type=button data-go=config>Set charter &amp; keys</button><button class="btn light" type=button data-go=run>Run a mission</button></div></div>
        <div class=card><div class=hdr><h4>Effective limits</h4><span id=govSrc class="tag recorded">default</span></div>
          <div class=gov id=govBox></div></div>
      </div>
      <div class=card><div class=hdr><h4>Last mission</h4><button class="btn light" type=button data-go=run>New mission</button></div>
        <div id=lastRun><div class=empty>No mission run in this session yet. Each run shows its tasks, audit verdicts and scores here.</div></div></div>
    </div>

    <div class="panel hide" data-panel=run>
      <div class=card>
        <div class=hdr><h4>New mission</h4><span class=small>planned &rarr; priced &rarr; gated &rarr; executed &rarr; audited</span></div>
        <label for=goal>Goal</label><textarea id=goal placeholder="Summarize this week's agent-infrastructure releases."></textarea>
        <div class=row><button class="btn dark" id=btn_run type=button><svg class=ico viewBox="0 0 24 24"><path d="M6 4l14 8-14 8z"/></svg>Run mission</button><span class=msg id=runMsg></span></div>
        <div id=runOut class=hide></div>
        <div class=empty id=runEmpty>Results appear here: each task with its audit verdict, score and output.</div>
      </div>
    </div>

    <div class="panel hide" data-panel=config>
      <div class=two>
        <div class=card><h4>Charter</h4><p class=lead>The YAML every mission in this workspace is gated against.</p>
          <label for=cfgCharter>charter.yaml</label><textarea id=cfgCharter rows=14 placeholder="mission: Ship weekly research briefs.&#10;fiscal_boundaries:&#10;  daily_burn_max_usd: 5.0&#10;  max_task_cost_usd: 0.50"></textarea>
          <div class=row><button class="btn dark" id=btn_saveCharter type=button>Save charter</button><span class=msg id=chMsg></span></div></div>
        <div class=card><h4>Model keys</h4><p class=lead>Encrypted at rest; used only for your missions. Leave blank to keep the current key.</p>
          <label for=cfgAnthropic>Anthropic API key</label><input id=cfgAnthropic placeholder="sk-ant-&hellip;" autocomplete=off>
          <label for=cfgOpenAI>OpenAI API key</label><input id=cfgOpenAI placeholder="sk-&hellip;" autocomplete=off>
          <div class=row><button class="btn dark" id=btn_saveConfig type=button>Save changes</button><span class=msg id=cfgMsg></span></div></div>
      </div>
    </div>

    <div class="panel hide" data-panel=settings>
      <div class="card danger">
        <div class=dz><div><h5>Rotate workspace key</h5><p>The key is the only credential and is stored hashed. If it leaks, rotate &mdash; the old key stops working immediately.</p></div>
          <button class="btn light" id=btn_rotate type=button>Rotate key</button></div>
        <div class=row><span class=msg id=rotMsg></span></div>
        <div id=rotBox class=hide><div class=key id=rotKey></div></div>
        <div class=dz><div><h5>Delete workspace</h5><p>Removes this workspace, its stored provider keys, its ledger and its audit trail. It cannot be undone.</p></div>
          <button class="btn warn" id=btn_delete type=button>Delete workspace</button></div>
        <div class=row><span class=msg id=delMsg></span></div>
      </div>
    </div>
  </section>
</div>

<script nonce=__CSP_NONCE__>
var KEY=localStorage.getItem("sov_key")||"";
var CURRENT_ID="";
var SVG="http://www.w3.org/2000/svg";
function h(){return KEY?{"X-Tenant-Key":KEY,"Content-Type":"application/json"}:{"Content-Type":"application/json"}}
function el(id){return document.getElementById(id)}
function mk(tag,cls,text){var e=document.createElement(tag);if(cls)e.className=cls;if(text!=null)e.textContent=text;return e}
function cents(c){return c<100?(c+"¢"):("$"+(c%100?(c/100).toFixed(2):(c/100).toLocaleString()))}
function usd(c){return "$"+(c/100).toFixed(2)}
function setMsg(id,cls,text){var m=el(id);m.className="msg"+(cls?" "+cls:"");m.textContent=text}
function json(r){return r.json().then(function(d){return {ok:r.ok,d:d}})}
function check(){var s=document.createElementNS(SVG,"svg");s.setAttribute("class","ico");s.setAttribute("viewBox","0 0 24 24");
  var p=document.createElementNS(SVG,"path");p.setAttribute("d","M20 6 9 17l-5-5");s.appendChild(p);return s}

/* ---------- nav surface on scroll ---------- */
var nav=el("nav");
function onScroll(){nav.classList.toggle("scrolled",window.scrollY>24)}
window.addEventListener("scroll",onScroll,{passive:true});onScroll();

/* ---------- plans: pricing grid + signup select, one source ---------- */
var FEAT={governance:"Charter governance and CFO gate",audit:"Judge audit with proof hashes",dashboard:"Live decision dashboard",
  backends:"Pluggable execution backends",mcp:"MCP tool connectors",earning:"Paid job intake and settlement"};
var FOR={free:"For trying governance on a real agent.",pro:"For a team running agents in production.",team:"For agents that take paid work end to end."};
fetch("/saas/plans").then(function(r){return r.json()}).then(function(d){
  var sel=el("suPlan"),grid=el("plans");sel.textContent="";grid.textContent="";
  (d.plans||[]).forEach(function(p,i){
    var o=mk("option",null,p.name+" — $"+(p.price_cents_month/100).toFixed(0)+"/mo · "+p.max_missions_per_day+" missions/day");
    o.value=p.name;sel.appendChild(o);
    var hot=i===1,c=mk("div","plan"+(hot?" hot":""));
    if(hot)c.appendChild(mk("span","flag","Most teams"));
    c.appendChild(mk("div","nm",p.name));
    c.appendChild(mk("p","for",FOR[p.name]||""));
    var pr=mk("div","pr","$"+(p.price_cents_month/100).toFixed(0));pr.appendChild(mk("small",null,"/ month"));c.appendChild(pr);
    var lim=mk("div","lim");
    [["Missions / day",p.max_missions_per_day.toLocaleString()],["Daily spend ceiling",cents(p.max_daily_spend_cents)],["Seats",String(p.seats)]]
      .forEach(function(x){var r=mk("div");r.appendChild(mk("span",null,x[0]));r.appendChild(mk("b",null,x[1]));lim.appendChild(r)});
    c.appendChild(lim);
    var ul=mk("ul");
    (p.features||[]).forEach(function(f){var li=mk("li");li.appendChild(check());li.appendChild(document.createTextNode(FEAT[f]||f));ul.appendChild(li)});
    c.appendChild(ul);
    var b=mk("button","btn "+(hot?"dark":"light"),i===0?"Start free":"Start with "+p.name);b.type="button";
    b.addEventListener("click",function(){sel.value=p.name;el("start").scrollIntoView();el("suName").focus({preventScroll:true})});
    c.appendChild(b);grid.appendChild(c);
  });
}).catch(function(){});

/* ---------- the live run ---------- */
function amountOf(s){
  var d=s.detail||{};
  if(d.estimated_cents!=null)return cents(d.estimated_cents);
  if(d.token_cents!=null)return cents(d.token_cents);
  if(d.score!=null)return "score "+d.score;
  if(d.trust_score!=null)return d.trust_score+" / "+d.threshold;
  return "";
}
function barcode(hex){
  var svg=el("rcBarcode");while(svg.firstChild)svg.removeChild(svg.firstChild);
  var x=0;for(var i=0;i<hex.length&&x<256;i++){
    var v=parseInt(hex[i],16),w=1+(v%3),gap=1+((v>>2)%3);
    var r=document.createElementNS(SVG,"rect");
    r.setAttribute("x",x);r.setAttribute("y",0);r.setAttribute("width",w);r.setAttribute("height",38);r.setAttribute("fill","#121417");
    svg.appendChild(r);x+=w+gap;
  }
}
function renderRun(d){
  var steps=d.steps||[],tb=el("demoOut");tb.textContent="";
  var charter={},spent=0,refused=0,hash="",lines=el("rcLines");lines.textContent="";
  steps.forEach(function(s,i){
    var tr=mk("tr",s.decision);tr.style.animationDelay=(i*45)+"ms";
    var a=mk("td","stg");a.appendChild(mk("i"));a.appendChild(document.createTextNode(s.stage));tr.appendChild(a);
    var b=mk("td");b.appendChild(mk("span","tag "+s.decision,s.decision));tr.appendChild(b);
    tr.appendChild(mk("td",null,s.headline));
    tr.appendChild(mk("td","num",amountOf(s)));
    tb.appendChild(tr);
    var dt=s.detail||{};
    if(s.stage==="charter")charter=dt;
    if(s.decision==="refused")refused++;
    if(s.stage==="budget"){
      var ln=mk("div","ln"+(s.decision==="refused"?" x":""));
      ln.appendChild(mk("span",null,dt.task_id));
      ln.appendChild(mk("span",null,s.decision==="refused"?"refused":cents(dt.estimated_cents)));
      lines.appendChild(ln);
    }
    if(s.stage==="ledger"&&dt.token_cents!=null)spent=dt.token_cents;
    if(dt.proof_hash&&!hash)hash=dt.proof_hash;
  });
  el("stDecisions").textContent=steps.length;
  el("stRefused").textContent=refused;
  el("stSpend").textContent=cents(spent);
  if(charter.daily_burn_max_usd)el("stSpendD").textContent="of a $"+charter.daily_burn_max_usd.toFixed(2)+" daily cap";
  el("logCount").textContent=steps.length+" decisions · "+refused+" refused · run at "+new Date().toLocaleTimeString([], {hour:"2-digit",minute:"2-digit",second:"2-digit"});
  if(d.disclaimer)el("demoDisc").textContent=d.disclaimer;
  el("rcTotal").textContent=cents(spent);
  if(charter.max_budget_usd)el("rcBal").textContent="against a $"+charter.max_budget_usd.toFixed(2)+" budget";
  el("rcDate").textContent=new Date().toLocaleDateString([], {month:"short",day:"numeric"});
  if(hash){barcode(hash);el("rcFoot").textContent=hash.slice(0,16)+"…"+hash.slice(-8);
    var hb=el("hashBox");hb.textContent="";hb.appendChild(mk("span","dim","sha256 "));hb.appendChild(document.createTextNode(hash))}
}
function runDemo(scroll){
  var btn=el("btn_demo");btn.disabled=true;el("demoHint").textContent="Running the governed mission…";
  return fetch("/saas/demo").then(function(r){return r.json()}).then(function(d){
    renderRun(d);
    el("demoHint").textContent=d.refusals+" of "+(d.steps||[]).length+" decisions were refusals — see the log and receipt below.";
    btn.disabled=false;
    if(scroll)el("live").scrollIntoView();
  }).catch(function(e){el("demoHint").textContent="The run did not complete: "+e;btn.disabled=false});
}
function copyCmd(){
  var t=el("cmdText").textContent,b=el("btn_copy");
  if(!navigator.clipboard){b.textContent="Select to copy";return}
  navigator.clipboard.writeText(t).then(function(){b.textContent="Copied";setTimeout(function(){b.textContent="Copy"},1600)},
    function(){b.textContent="Select to copy"});
}

/* ---------- app shell ---------- */
var TITLES={overview:["Overview","Today's usage against your plan and your charter."],
  run:["Run a mission","Planned, priced against your charter, executed on your key, then audited."],
  config:["Charter & keys","What every mission in this workspace is gated against, and the keys it runs on."],
  settings:["Settings","Credential and data controls for this workspace."]};
function show(p){
  document.querySelectorAll("#tabs .tab").forEach(function(t){t.setAttribute("aria-selected",String(t.getAttribute("data-p")===p))});
  document.querySelectorAll(".panel").forEach(function(x){x.classList.toggle("hide",x.getAttribute("data-panel")!==p)});
  el("pTitle").textContent=TITLES[p][0];el("pSub").textContent=TITLES[p][1];
}
document.querySelectorAll("#tabs .tab").forEach(function(t){t.addEventListener("click",function(){show(t.getAttribute("data-p"))})});
document.querySelectorAll("[data-go]").forEach(function(b){b.addEventListener("click",function(){show(b.getAttribute("data-go"))})});
function enterApp(){document.body.classList.add("authed");el("app").classList.remove("hide");window.scrollTo(0,0)}
function leaveApp(){document.body.classList.remove("authed");el("app").classList.add("hide")}
function signout(){localStorage.removeItem("sov_key");KEY="";leaveApp();el("inKey").value="";setMsg("siMsg","ok","Signed out.")}

/* ---------- workspace ---------- */
function signup(){
  setMsg("suMsg","","Creating…");
  fetch("/saas/tenants",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({name:el("suName").value,plan:el("suPlan").value})})
   .then(json).then(function(x){
     if(!x.ok){setMsg("suMsg","err",x.d.detail||"Error");return}
     el("keyBox").classList.remove("hide");el("apiKey").textContent=x.d.api_key;
     el("inKey").value=x.d.api_key;setMsg("suMsg","ok","Created. Save the key, then sign in.");
   }).catch(function(e){setMsg("suMsg","err",String(e))})
}
function signin(){
  KEY=el("inKey").value.trim();
  if(!KEY){setMsg("siMsg","err","Paste your workspace key first.");return}
  fetch("/saas/tenants/me",{headers:h()}).then(json).then(function(x){
    if(!x.ok){setMsg("siMsg","err",x.d.detail||"Invalid key");return}
    localStorage.setItem("sov_key",KEY);setMsg("siMsg","","");renderMe(x.d);enterApp();
  }).catch(function(e){setMsg("siMsg","err",String(e))})
}
function pill(text,dot){
  // textContent, not innerHTML: the workspace name is whatever the tenant typed at
  // signup, and the tenant API key sits in localStorage one DOM sink away.
  var s=mk("span","pill");if(dot)s.appendChild(mk("span","dot"));
  s.appendChild(document.createTextNode(text));return s;
}
function meter(id,frac){var b=el(id);b.style.width=Math.max(0,Math.min(100,frac*100)).toFixed(1)+"%"}
function renderMe(d){
  var t=d.tenant||{},c=t.config||{},u=d.usage||{},pl=d.plan||{},g=d.governance||{};
  CURRENT_ID=t.id||"";
  el("wsName").textContent=t.name||"Workspace";el("wsAv").textContent=(t.name||"W").trim().charAt(0).toUpperCase()||"W";
  el("wsPlan").textContent=(t.plan||"")+" plan";
  var ta=el("cfgCharter");if(document.activeElement!==ta){ta.value=c.charter_yaml||""}
  var row=el("meRow");row.textContent="";row.appendChild(pill(t.plan+" plan",1));
  var mis=u.missions||0,mmax=pl.max_missions_per_day||0,sp=u.spend_cents||0,smax=pl.max_daily_spend_cents||0;
  el("tMis").textContent=mis;el("tMisD").textContent="of "+mmax.toLocaleString()+" on your plan";meter("mMis",mmax?mis/mmax:0);
  el("tMisPct").textContent=mmax?Math.round(100*mis/mmax)+"%":"";
  el("tSp").textContent=usd(sp);el("tSpD").textContent="of "+cents(smax)+" plan ceiling";meter("mSp",smax?sp/smax:0);
  el("tSpPct").textContent=smax?Math.round(100*sp/smax)+"%":"";
  var keys=(c.anthropic_api_key?1:0)+(c.openai_api_key?1:0);
  el("tKeys").textContent=keys;el("tKeys").appendChild(mk("small",null," / 2"));
  el("tKeysD").textContent=[c.anthropic_api_key?"Anthropic":"",c.openai_api_key?"OpenAI":""].filter(Boolean).join(" · ")||"none configured";
  el("tCap").textContent=g.daily_burn_max_usd?("$"+g.daily_burn_max_usd.toFixed(2)):"none";
  el("tCapD").textContent=g.source==="charter"?"from your charter":"platform default";
  el("nextLead").textContent=!keys?"Add a model key so missions can run. Without one, every mission is refused.":
    (!c.charter_yaml?"Write a charter to set your own budget and ceilings, or run on the platform defaults.":
     "Your workspace is configured. Run a mission and its audit verdicts land here.");
  renderGovernance(g);
}
function renderGovernance(g){
  var box=el("govBox"),src=el("govSrc");box.textContent="";
  if(g.source==="error"){src.className="tag fail";src.textContent="charter error";
    var e=mk("div");e.appendChild(mk("span",null,"Charter will not load — missions are refused"));box.appendChild(e);
    box.appendChild(mk("p","msg err",g.error));return}
  src.className="tag "+(g.source==="charter"?"pass":"recorded");src.textContent=g.source==="charter"?"your charter":"default";
  [["Daily burn cap",g.daily_burn_max_usd?("$"+g.daily_burn_max_usd.toFixed(2)):"none"],
   ["Budget ceiling",g.max_budget_usd?("$"+g.max_budget_usd.toFixed(2)):"none"],
   ["Margin floor",Math.round((g.min_job_margin_ratio||0)*100)+"%"]].forEach(function(x){
    var r=mk("div");r.appendChild(mk("span",null,x[0]));r.appendChild(mk("b",null,x[1]));box.appendChild(r)});
}
function refreshMe(){fetch("/saas/tenants/me",{headers:h()}).then(function(r){return r.json()}).then(renderMe)}
function saveConfig(ev){
  var mid=(ev&&ev.currentTarget&&ev.currentTarget.id==="btn_saveCharter")?"chMsg":"cfgMsg";
  setMsg(mid,"","Saving…");
  var body={};if(el("cfgAnthropic").value)body.anthropic_api_key=el("cfgAnthropic").value;
  if(el("cfgOpenAI").value)body.openai_api_key=el("cfgOpenAI").value;
  if(el("cfgCharter").value)body.charter_yaml=el("cfgCharter").value;
  fetch("/saas/tenants/me/config",{method:"PUT",headers:h(),body:JSON.stringify(body)})
   .then(json).then(function(x){
     if(!x.ok){setMsg(mid,"err",x.d.detail||"Error");return}
     setMsg(mid,"ok","Saved.");el("cfgAnthropic").value="";el("cfgOpenAI").value="";refreshMe();
   }).catch(function(e){setMsg(mid,"err",String(e))})
}
function renderResults(d){
  var out=el("runOut");out.textContent="";
  var audits={};(d.audits||[]).forEach(function(a){audits[a.task_id]=a});
  var sum=mk("div","sum");sum.appendChild(mk("span","tag "+(d.all_passed?"pass":"fail"),d.all_passed?"audit passed":"audit flagged"));
  sum.appendChild(document.createTextNode((d.tasks||[]).length+" tasks · "+(d.goal||"")));out.appendChild(sum);
  var list=mk("div","results");
  (d.tasks||[]).forEach(function(t){
    var a=audits[t.task_id],r=mk("div","res"),hd=mk("div","rh");
    hd.appendChild(mk("span","id",t.task_id));hd.appendChild(mk("span","sp"));
    if(a&&a.score!=null)hd.appendChild(mk("span","sc","score "+Number(a.score).toFixed(2)));
    var ok=a?a.passed:t.success;hd.appendChild(mk("span","tag "+(ok?"pass":"fail"),ok?"pass":"fail"));
    r.appendChild(hd);
    if(a&&a.reason)r.appendChild(mk("div","why",a.reason));
    r.appendChild(mk("pre",null,t.output||"(no output)"));list.appendChild(r);
  });
  out.appendChild(list);out.classList.remove("hide");el("runEmpty").classList.add("hide");
  var lr=el("lastRun");lr.textContent="";lr.appendChild(sum.cloneNode(true));
  var g=mk("div","gov");(d.tasks||[]).forEach(function(t){var a=audits[t.task_id],r=mk("div");
    r.appendChild(mk("span",null,t.task_id));r.appendChild(mk("b",null,a&&a.score!=null?("score "+Number(a.score).toFixed(2)+(a.passed?" · pass":" · fail")):(t.success?"done":"failed")));g.appendChild(r)});
  lr.appendChild(g);
}
function run(){
  var b=el("btn_run");b.disabled=true;setMsg("runMsg","","Planning, pricing and running…");
  fetch("/saas/tenants/me/missions",{method:"POST",headers:h(),body:JSON.stringify({goal:el("goal").value})})
   .then(json).then(function(x){
     b.disabled=false;
     if(!x.ok){setMsg("runMsg","err",x.d.detail||"Error");return}
     setMsg("runMsg","","");renderResults(x.d);refreshMe();
   }).catch(function(e){b.disabled=false;setMsg("runMsg","err",String(e))})
}
function rotate(){
  if(!confirm("Rotate the workspace key? The current key stops working immediately."))return;
  setMsg("rotMsg","","Rotating…");
  fetch("/saas/tenants/me/rotate-key",{method:"POST",headers:h()})
   .then(json).then(function(x){
     if(!x.ok){setMsg("rotMsg","err",x.d.detail||"Error");return}
     KEY=x.d.api_key;localStorage.setItem("sov_key",KEY);el("inKey").value=KEY;
     el("rotKey").textContent=x.d.api_key;el("rotBox").classList.remove("hide");
     setMsg("rotMsg","ok","Rotated — save the new key, it is shown once.");
   }).catch(function(e){setMsg("rotMsg","err",String(e))})
}
function destroy(){
  var id=CURRENT_ID||"";
  var typed=prompt("This deletes the workspace, its stored provider keys, its ledger and its audit trail, and cannot be undone.\n\nType the workspace id to confirm:\n"+id);
  if(!typed)return;
  setMsg("delMsg","","Deleting…");
  fetch("/saas/tenants/me",{method:"DELETE",headers:h(),body:JSON.stringify({confirm:typed.trim()})})
   .then(json).then(function(x){
     if(!x.ok){setMsg("delMsg","err",x.d.detail||"Error");return}
     localStorage.removeItem("sov_key");KEY="";leaveApp();
     setMsg("siMsg","ok","Workspace "+x.d.deleted+" deleted.");
   }).catch(function(e){setMsg("delMsg","err",String(e))})
}
el("btn_demo").addEventListener("click",function(){runDemo(true)});
el("btn_copy").addEventListener("click",copyCmd);
el("btn_rotate").addEventListener("click",rotate);
el("btn_delete").addEventListener("click",destroy);
el("btn_signup").addEventListener("click",signup);
el("btn_signin").addEventListener("click",signin);
el("inKey").addEventListener("keydown",function(e){if(e.key==="Enter")signin()});
el("btn_saveConfig").addEventListener("click",saveConfig);
el("btn_run").addEventListener("click",run);
el("btn_signout").addEventListener("click",signout);
el("btn_signout2").addEventListener("click",signout);
el("btn_saveCharter").addEventListener("click",saveConfig);
el("appBrand").addEventListener("click",function(e){e.preventDefault();show("overview")});
runDemo(false);
if(KEY){el("inKey").value=KEY;signin()}
</script></body></html>"""


def main() -> int:  # pragma: no cover
    import uvicorn

    # Serving over a network means serving other people; preflight is not optional here.
    uvicorn.run(create_saas_app(hosted=True),
                host="0.0.0.0", port=int(os.getenv("PORT", "8020")))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
