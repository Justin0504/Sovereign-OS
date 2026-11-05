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
<link rel=preconnect href=https://fonts.googleapis.com>
<link rel=preconnect href=https://fonts.gstatic.com crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Hanken+Grotesk:wght@400;500;600;700&family=Instrument+Serif:ital@0;1&family=JetBrains+Mono:wght@400;500&display=swap" rel=stylesheet>
<style nonce=__CSP_NONCE__>
/* One visual world, deliberately light: a sky the product sits under, a white working
   surface floating on it, and a ledger receipt as the closing object. Every colour is
   painted explicitly so the page holds on any host ground. */
:root{
  --sky-1:#c6daf2; --sky-2:#e4ecf7; --sky-3:#f6efe3;
  --canvas:#f3f3f1; --paper:#fff; --tint:#f7f7f5; --line:#e6e6e3; --line-2:#d9d9d5;
  --ink:#121417; --soft:#555b64; --mut:#8a9098;
  --pine:#0c5c46; --pine-2:#0a4a39; --pine-soft:#e6f1ec;
  --ok:#12805c; --ok-soft:#e5f3ec; --bad:#c8372a; --bad-soft:#fbe9e6; --amber:#b45309;
  --sans:'Hanken Grotesk',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
  --serif:'Instrument Serif',Georgia,'Times New Roman',serif;
  --mono:'JetBrains Mono',ui-monospace,SFMono-Regular,Menlo,monospace;
  --ease:cubic-bezier(.2,.7,.2,1);
  --shadow:0 1px 2px rgba(18,20,23,.04),0 8px 24px -8px rgba(18,20,23,.10);
}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{margin:0;font-family:var(--sans);background:var(--canvas);color:var(--ink);
     font-size:16px;line-height:1.55;-webkit-font-smoothing:antialiased}
a{color:inherit}
button{font:inherit}
:focus-visible{outline:2px solid var(--pine);outline-offset:2px}
.hide{display:none!important}
.mono{font-family:var(--mono)}
.shell{max-width:1160px;margin:0 auto;padding:0 24px}

/* ---------------- nav ---------------- */
.nav{position:absolute;inset:0 0 auto 0;z-index:5}
.nav .shell{display:flex;align-items:center;justify-content:space-between;height:68px}
.brand{display:flex;align-items:center;gap:10px;text-decoration:none;font-weight:700;
       letter-spacing:.02em;font-size:14px;text-transform:uppercase}
.brand svg{width:22px;height:22px}
.links{display:flex;gap:32px;font-size:14px;color:var(--soft)}
.links a{text-decoration:none;transition:color .15s}
.links a:hover{color:var(--ink)}
.cta-pill{display:inline-flex;align-items:center;gap:6px;background:var(--paper);color:var(--ink);
  text-decoration:none;font-size:14px;font-weight:600;padding:9px 14px;border-radius:8px;
  box-shadow:0 1px 2px rgba(18,20,23,.08);transition:transform .15s var(--ease)}
.cta-pill:hover{transform:translateY(-1px)}

/* ---------------- sky hero ---------------- */
.sky{position:relative;overflow:hidden;padding:150px 0 220px;text-align:center;
  background:linear-gradient(180deg,var(--sky-1) 0%,var(--sky-2) 52%,var(--sky-3) 88%,var(--canvas) 100%)}
.cloud{position:absolute;pointer-events:none;
  background-image:radial-gradient(circle,#fff 1.6px,transparent 2px),
    radial-gradient(closest-side,rgba(255,255,255,.55),transparent);
  background-size:5px 5px,100% 100%;
  -webkit-mask-image:radial-gradient(closest-side,#000 30%,transparent 100%);
          mask-image:radial-gradient(closest-side,#000 30%,transparent 100%)}
.cloud.warm{background-image:radial-gradient(circle,#f3c58a 1.6px,transparent 2px),radial-gradient(closest-side,rgba(250,226,190,.6),transparent)}
.c1{width:620px;height:260px;left:-160px;top:300px}
.c2{width:520px;height:220px;right:-120px;top:250px}
.c3{width:760px;height:240px;left:22%;bottom:30px;opacity:.85}
.c4{width:420px;height:160px;right:8%;bottom:90px}
.c5{width:360px;height:140px;left:6%;bottom:120px}
.c6{width:300px;height:120px;left:58%;top:150px;opacity:.6}
.sky .shell{position:relative;z-index:1}
.eyebrow{display:inline-flex;align-items:center;gap:16px;font-size:12px;letter-spacing:.18em;
  text-transform:uppercase;color:var(--soft);font-weight:600;margin:0 0 28px}
.eyebrow::before,.eyebrow::after{content:"";width:56px;height:1px;background:currentColor;opacity:.45}
h1{margin:0 auto;font-weight:500;font-size:clamp(3rem,8.4vw,7.4rem);line-height:.98;
   letter-spacing:-.045em;max-width:15ch;text-wrap:balance}
h1 em{font-family:var(--serif);font-style:italic;font-weight:400;letter-spacing:-.02em;
   font-size:1.08em}
.give{margin:40px 0 14px;font-size:14px;color:var(--soft)}
.cmd{display:flex;align-items:center;gap:10px;max-width:560px;margin:0 auto;background:var(--paper);
  border-radius:12px;padding:8px 8px 8px 16px;box-shadow:var(--shadow);text-align:left}
.cmd .p{color:var(--mut);font-family:var(--mono);font-size:13px}
.cmd code{flex:1;font-family:var(--mono);font-size:13px;white-space:nowrap;overflow:hidden;
  text-overflow:ellipsis}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:6px;border:none;cursor:pointer;
  border-radius:8px;font-weight:600;font-size:14px;padding:10px 16px;min-height:40px;
  transition:background .15s var(--ease),transform .15s var(--ease)}
.btn:active{transform:translateY(1px)}
.btn.dark{background:var(--ink);color:#fff}.btn.dark:hover{background:#2a2e35}
.btn.light{background:var(--tint);color:var(--ink);border:1px solid var(--line)}
.btn.light:hover{background:#efefec}
.btn.pine{background:var(--pine);color:#fff}.btn.pine:hover{background:var(--pine-2)}
.btn.warn{background:var(--bad);color:#fff}.btn.warn:hover{background:#a92d22}
.btn[disabled]{opacity:.6;cursor:progress}
.hint{margin:14px 0 0;font-size:13px;color:var(--soft)}

/* ---------------- bento ---------------- */
.bento{position:relative;z-index:2;margin-top:-150px;background:var(--paper);
  border:1px solid var(--line);border-radius:4px;box-shadow:0 30px 60px -30px rgba(18,20,23,.18)}
.brow{display:grid;grid-template-columns:minmax(0,5fr) minmax(0,7fr);border-top:1px solid var(--line)}
.brow:first-child{border-top:none}
.brow.split{grid-template-columns:1fr 1fr}
.brow.quad{grid-template-columns:repeat(4,minmax(0,1fr))}
.cell{padding:36px 32px;min-width:0}
.cell+.cell{border-left:1px solid var(--line)}
.cell h3{margin:0 0 8px;font-size:22px;font-weight:600;letter-spacing:-.015em;display:flex;
  align-items:baseline;gap:10px}
.cell h3 small{font-size:12px;font-weight:500;color:var(--mut);letter-spacing:0}
.cell p{margin:0;color:var(--soft);font-size:15px;max-width:44ch}
.replaces{margin-top:18px;display:flex;gap:8px;align-items:center;flex-wrap:wrap;font-size:12px;color:var(--mut)}
.replaces span.r{border:1px solid var(--line);border-radius:6px;padding:2px 8px;color:var(--soft);
  text-decoration:line-through;text-decoration-color:rgba(200,55,42,.55)}
.vis{margin-top:24px}

/* charter YAML */
.yaml{background:#101317;color:#d7dce3;border-radius:10px;padding:18px 0;font-family:var(--mono);
  font-size:12.5px;line-height:1.75;overflow-x:auto;box-shadow:var(--shadow)}
.yaml .ln{display:grid;grid-template-columns:44px 1fr;white-space:pre}
.yaml .n{color:#525a66;text-align:right;padding-right:16px;user-select:none}
.yaml .k{color:#9cc9b8}.yaml .s{color:#e8c48f}.yaml .v{color:#f0f3f6}.yaml .c{color:#6b7480}
.yaml .hl{background:rgba(156,201,184,.08);box-shadow:inset 2px 0 0 #9cc9b8}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin-top:4px}
.chip{display:inline-flex;align-items:center;gap:8px;border:1px solid var(--line);border-radius:8px;
  padding:6px 10px;font-size:13px;background:var(--paper)}
.chip i{width:7px;height:7px;border-radius:50%;background:var(--ok);display:inline-block}
.chip.no i{background:var(--bad)}

/* approval card (CFO) */
.stack{position:relative;padding-top:4px}
.appr{background:var(--paper);border:1px solid var(--line);border-radius:12px;padding:14px 16px;
  box-shadow:var(--shadow);max-width:400px}
.appr+.appr{margin-top:-6px;margin-left:28px;position:relative}
.appr .top{display:flex;justify-content:space-between;font-size:12px;color:var(--mut)}
.appr .who{display:flex;gap:10px;align-items:center;font-weight:600;font-size:14px;margin-top:6px}
.badge{width:26px;height:26px;border-radius:7px;background:var(--ink);color:#fff;display:grid;
  place-items:center;font-size:11px;font-family:var(--mono)}
.appr .what{font-size:13px;color:var(--soft);margin:4px 0 10px 36px}
.appr .acts{display:flex;gap:8px;justify-content:flex-end}
.tag{font-family:var(--mono);font-size:11px;letter-spacing:.04em;padding:3px 9px;border-radius:999px;
  text-transform:uppercase;white-space:nowrap}
.tag.allowed{background:var(--ok-soft);color:var(--ok)}
.tag.refused{background:var(--bad-soft);color:var(--bad)}
.tag.recorded{background:var(--tint);color:var(--soft);border:1px solid var(--line)}
.meter{height:6px;border-radius:999px;background:var(--tint);border:1px solid var(--line);
  position:relative;margin:8px 0 4px}
.meter b{position:absolute;left:0;top:-1px;bottom:-1px;border-radius:999px;background:var(--ink)}
.meter .m55{width:55%}.meter .m40{width:40%;background:var(--pine)}.meter .m95{width:100%;background:var(--bad)}
.meter .tick{position:absolute;top:-5px;width:1px;height:14px;background:var(--bad)}
.meter .t40{left:42%}.meter .t80{left:80%}
.small{font-size:12px;color:var(--mut)}
.kv{display:flex;justify-content:space-between;font-size:13px;padding:6px 0;border-bottom:1px dashed var(--line)}
.kv:last-child{border-bottom:none}
.kv b{font-family:var(--mono);font-weight:500}
.mini{background:var(--tint);border:1px solid var(--line);border-radius:10px;padding:14px}
.bars{display:grid;gap:8px}
.bars div{display:grid;grid-template-columns:84px 1fr 30px;gap:8px;align-items:center;font-size:12px;color:var(--soft)}
.bars span.track{height:6px;background:#e9e9e5;border-radius:999px;overflow:hidden;display:block}
.bars span.fill{display:block;height:100%;background:var(--pine);border-radius:999px}
.f90{width:90%}.f85{width:85%}.f95{width:95%}
.hash{font-family:var(--mono);font-size:11.5px;line-height:1.6;word-break:break-all;color:var(--ink)}
.hash .dim{color:var(--mut)}
.ledgerlines{font-family:var(--mono);font-size:11.5px;line-height:1.9}
.ledgerlines div{display:flex;justify-content:space-between;border-bottom:1px solid var(--line)}
.ledgerlines div:last-child{border-bottom:none}
.ledgerlines .neg{color:var(--bad)}

/* ---------------- live run: activity + receipt ---------------- */
.band{padding:120px 0 40px}
h2.big{margin:0;font-weight:500;font-size:clamp(2.2rem,4.6vw,3.6rem);letter-spacing:-.035em;line-height:1.05;text-wrap:balance}
h2.big .fade{color:#b9bcc1}
h2.big em{font-family:var(--serif);font-weight:400;font-style:italic;letter-spacing:-.01em}
.sub{margin:14px 0 0;color:var(--soft);font-size:17px;max-width:60ch}
.stage{position:relative;margin-top:44px;display:grid;grid-template-columns:minmax(0,1fr) 300px;gap:0;align-items:start}
.win{background:var(--paper);border:1px solid var(--line);border-radius:12px;box-shadow:0 30px 60px -30px rgba(18,20,23,.2);overflow:hidden}
.bar{display:flex;align-items:center;gap:8px;padding:12px 16px;border-bottom:1px solid var(--line);font-size:14px;font-weight:600}
.lights{display:flex;gap:6px;margin-right:6px}
.lights i{width:10px;height:10px;border-radius:50%;display:block;background:#ff5f57}
.lights i:nth-child(2){background:#febc2e}.lights i:nth-child(3){background:#28c840}
.bar .sp{flex:1}
.stats{display:grid;grid-template-columns:repeat(3,1fr);margin:16px;border:1px solid var(--line);border-radius:10px;overflow:hidden}
.stat{padding:14px 16px}
.stat+.stat{border-left:1px solid var(--line)}
.stat .l{font-size:12px;color:var(--mut)}
.stat .n{font-size:26px;font-weight:600;letter-spacing:-.02em;font-variant-numeric:tabular-nums;line-height:1.2}
.stat .d{font-size:12px;color:var(--soft)}
.stat.first{background:var(--tint)}
.loghead{display:flex;align-items:baseline;gap:10px;padding:4px 16px 10px;font-size:13px}
.loghead b{font-weight:600}.loghead span{color:var(--mut)}
.tablewrap{overflow-x:auto}
table.log{width:100%;border-collapse:collapse;font-size:13px;min-width:620px}
table.log th{text-align:left;font-weight:500;color:var(--mut);font-size:12px;padding:8px 16px;border-bottom:1px solid var(--line)}
table.log td{padding:10px 16px;border-bottom:1px solid var(--line);vertical-align:top}
table.log tr:last-child td{border-bottom:none}
table.log td.stg{font-family:var(--mono);font-size:12px;color:var(--soft);white-space:nowrap}
table.log td.stg i{display:inline-block;width:6px;height:6px;border-radius:50%;background:var(--ok);margin-right:8px;vertical-align:middle}
table.log tr.refused td.stg i{background:var(--bad)}
table.log tr.recorded td.stg i{background:var(--mut)}
table.log tr.refused{background:#fdf6f4}
table.log td.num{font-family:var(--mono);font-size:12px;text-align:right;white-space:nowrap;color:var(--soft)}
.skel td{color:var(--mut)}
.disc{padding:12px 16px;border-top:1px solid var(--line);background:var(--tint);font-size:12px;color:var(--soft);line-height:1.5}

.receipt{position:relative;margin:70px 0 0 -40px;background:#fffefb;border-radius:4px;padding:22px 22px 18px;
  box-shadow:0 24px 48px -20px rgba(18,20,23,.28),0 2px 6px rgba(18,20,23,.06);transform:rotate(3deg);z-index:2;
  font-size:13px}
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
.stamp{width:62px;height:62px;border-radius:50%;border:2px solid rgba(12,92,70,.55);color:rgba(12,92,70,.75);
  display:grid;place-items:center;font-family:var(--mono);font-size:9.5px;letter-spacing:.12em;transform:rotate(-14deg);
  box-shadow:inset 0 0 0 3px #fffefb,inset 0 0 0 4px rgba(12,92,70,.35)}
.barcode{display:block;width:100%;height:38px;margin-top:16px}
.receipt .foot{text-align:center;font-size:10.5px;color:var(--mut);font-family:var(--mono);margin-top:6px;word-break:break-all}

/* ---------------- measured ---------------- */
.measured{padding:80px 0 30px;text-align:center}
.kicker{font-size:12px;letter-spacing:.18em;text-transform:uppercase;color:var(--mut);font-weight:600}
.facts{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));margin-top:28px;border-top:1px solid var(--line-2);border-bottom:1px solid var(--line-2)}
.fact{padding:26px 18px;text-align:left}
.fact+.fact{border-left:1px solid var(--line-2)}
.fact .n{font-size:40px;font-weight:500;letter-spacing:-.04em;line-height:1;font-variant-numeric:tabular-nums}
.fact .n em{font-family:var(--serif);font-weight:400;font-size:.9em}
.fact p{margin:10px 0 0;font-size:13px;color:var(--soft);max-width:28ch}
.src{margin-top:14px;font-size:12px;color:var(--mut)}
.src a{color:var(--soft)}

/* ---------------- pricing ---------------- */
.pricing{padding:100px 0 20px}
.plans{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px;margin-top:36px}
.plan{background:var(--paper);border:1px solid var(--line);border-radius:12px;padding:26px;display:flex;flex-direction:column}
.plan.hot{border-color:var(--ink);box-shadow:var(--shadow)}
.plan .nm{font-size:13px;text-transform:uppercase;letter-spacing:.12em;color:var(--soft);font-weight:600}
.plan .pr{font-size:44px;font-weight:500;letter-spacing:-.04em;margin:10px 0 2px;font-variant-numeric:tabular-nums}
.plan .pr small{font-size:15px;color:var(--mut);letter-spacing:0;font-weight:400}
.plan ul{list-style:none;padding:0;margin:16px 0 22px;font-size:14px;color:var(--soft);display:grid;gap:8px;flex:1}
.plan li{display:flex;gap:8px}
.plan li::before{content:"";width:12px;height:12px;margin-top:5px;border-radius:3px;background:var(--pine-soft);border:1px solid rgba(12,92,70,.3);flex:none}

/* ---------------- start / console ---------------- */
.start{padding:100px 0 60px}
.cols{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px;margin-top:36px}
.card{background:var(--paper);border:1px solid var(--line);border-radius:12px;padding:26px}
.card h4{margin:0 0 4px;font-size:18px;font-weight:600;letter-spacing:-.01em}
.card .lead{margin:0 0 6px;font-size:14px;color:var(--soft)}
.card.danger{border-color:#efd0cb}
label{display:block;font-size:13px;color:var(--soft);margin:16px 0 6px;font-weight:500}
input,textarea,select{width:100%;padding:11px 12px;border:1px solid var(--line-2);border-radius:8px;
  font:inherit;font-size:14px;background:var(--paper);color:var(--ink);transition:border-color .15s,box-shadow .15s}
input:focus,textarea:focus,select:focus{outline:none;border-color:var(--pine);box-shadow:0 0 0 3px var(--pine-soft)}
textarea{resize:vertical;min-height:96px;font-family:var(--mono);font-size:12.5px;line-height:1.6}
.row{display:flex;gap:12px;flex-wrap:wrap;align-items:center;margin-top:16px}
.key{font-family:var(--mono);font-size:12px;background:var(--tint);border:1px solid var(--line);border-radius:8px;padding:12px;word-break:break-all;margin-top:8px}
.pill{display:inline-flex;gap:6px;align-items:center;font-size:12px;border:1px solid var(--line);border-radius:999px;
  padding:4px 12px;color:var(--soft);background:var(--paper);font-family:var(--mono)}
.dot{width:6px;height:6px;border-radius:50%;background:var(--ok)}
.msg{font-size:13px;min-height:1em}
.msg.err{color:var(--bad)}.msg.ok{color:var(--ok)}
.mut{color:var(--mut);font-size:12px;max-width:65ch;margin:14px 0 0}
.out{white-space:pre-wrap;background:var(--tint);border:1px solid var(--line);border-radius:8px;padding:16px;
  font-family:var(--mono);font-size:12px;line-height:1.6;margin-top:16px;max-height:380px;overflow:auto}
#app{margin-top:16px;display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}
#app .wide{grid-column:1/-1}

footer{border-top:1px solid var(--line-2);margin-top:80px;padding:28px 0 40px;font-size:13px;color:var(--mut)}
footer .shell{display:flex;justify-content:space-between;gap:16px;flex-wrap:wrap}
footer nav{display:flex;gap:20px}
footer a{text-decoration:none;color:var(--soft)}
footer a:hover{color:var(--ink)}

@keyframes rise{from{opacity:.001;transform:translateY(10px)}to{opacity:1;transform:none}}
.sky h1,.sky .give,.sky .cmd{animation:rise .8s var(--ease) both}
.sky .give{animation-delay:.12s}.sky .cmd{animation-delay:.2s}
table.log tbody tr{animation:rise .4s var(--ease) both}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}html{scroll-behavior:auto}}

@media (max-width:980px){
  .links{display:none}
  .brow,.brow.split{grid-template-columns:minmax(0,1fr)}
  .brow.quad{grid-template-columns:repeat(2,minmax(0,1fr))}
  .cell+.cell{border-left:none;border-top:1px solid var(--line)}
  .brow.quad .cell:nth-child(2n){border-left:1px solid var(--line)}
  .brow.quad .cell:nth-child(2){border-top:none}
  .stage{grid-template-columns:minmax(0,1fr)}
  .receipt{margin:-20px auto 0;max-width:320px;transform:rotate(2deg)}
  .facts{grid-template-columns:repeat(2,minmax(0,1fr))}
  .fact:nth-child(3){border-left:none}
  .fact:nth-child(n+3){border-top:1px solid var(--line-2)}
  .plans,.cols,#app{grid-template-columns:minmax(0,1fr)}
}
@media (max-width:600px){
  .shell{padding:0 16px}
  .sky{padding:120px 0 180px}
  .brow.quad{grid-template-columns:minmax(0,1fr)}
  .cmd{flex-wrap:wrap}.cmd code{flex-basis:100%}
  .eyebrow{gap:10px;letter-spacing:.14em}.eyebrow::before,.eyebrow::after{width:22px}
  .brow.quad .cell:nth-child(2n){border-left:none}
  .brow.quad .cell:nth-child(2){border-top:1px solid var(--line)}
  .cell{padding:28px 20px}
  .cmd code{font-size:11.5px}
  .stats .n{font-size:20px}
  .fact .n{font-size:32px}
}
</style></head><body>

<header class=nav><div class=shell>
  <a class=brand href="#top" aria-label="Sovereign-OS home">
    <svg viewBox="0 0 24 24" aria-hidden=true><rect x="2" y="2" width="20" height="5" rx="1.5" fill="#121417"/><rect x="2" y="9.5" width="5.5" height="5" rx="1.2" fill="#121417"/><rect x="9.25" y="9.5" width="5.5" height="5" rx="1.2" fill="#0c5c46"/><rect x="16.5" y="9.5" width="5.5" height="5" rx="1.2" fill="#121417"/><rect x="2" y="17" width="20" height="5" rx="1.5" fill="none" stroke="#121417" stroke-width="1.5"/></svg>
    Sovereign-OS
  </a>
  <nav class=links aria-label=Primary>
    <a href="#product">Product</a><a href="#live">Live run</a><a href="#pricing">Pricing</a>
    <a href="https://arxiv.org/abs/2603.14011">Paper</a><a href="https://github.com/Justin0504/Sovereign-OS">GitHub</a>
  </nav>
  <a class=cta-pill href="#start">Get started <span aria-hidden=true>&rarr;</span></a>
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
      <span class=p aria-hidden=true>&rsaquo;</span>
      <code id=cmdText>curl -s https://sovereign-saas-production.up.railway.app/saas/demo</code>
      <button class="btn light" id=btn_copy type=button>Copy</button>
      <button class="btn dark" id=btn_demo type=button>Run it</button>
    </div>
    <p class=hint id=demoHint>Real charter, real CFO gate, real proof hashes. The run appears below.</p>
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
      <div class="cell vis-cell">
        <div class=yaml role=img aria-label="Example charter: daily burn max 2 dollars, budget 25 dollars, per-task ceiling 40 cents">
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

    <div class="brow split">
      <div class=cell>
        <h3>CFO gate <small>before anything runs</small></h3>
        <p>Every task is priced against the charter first. Over the ceiling, it never starts &mdash;
          no tokens burned, no half-finished work to clean up.</p>
        <div class="vis stack">
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
        <div class="vis mini">
          <div class=kv><span>worker-demo &middot; TrustScore</span><b>55</b></div>
          <div class=meter><b class=m55></b><span class="tick t80"></span></div>
          <div class=chips>
            <span class=chip><i></i>read_files &middot; needs 10</span>
            <span class="chip no"><i></i>spend_usd &middot; needs 80</span>
          </div>
        </div>
      </div>
    </div>

    <div class="brow quad">
      <div class=cell>
        <h3>Audit</h3>
        <p>A judge scores each deliverable against a rubric. The deliverable is fenced as data, never instructions.</p>
        <div class="vis mini bars" aria-label="Judge scores from the demo run">
          <div>task-1<span class=track><span class="fill f90"></span></span><span class=mono>0.90</span></div>
          <div>task-2<span class=track><span class="fill f90"></span></span><span class=mono>0.90</span></div>
          <div>task-3<span class=track></span><span class=mono>&mdash;</span></div>
        </div>
      </div>
      <div class=cell>
        <h3>Proof hash</h3>
        <p>Every audit is sealed with SHA-256 over the task, output and verdict. Change a byte, the hash breaks.</p>
        <div class="vis mini hash" id=hashBox><span class=dim>sha256</span> 4c8dac89494a5529e29230e9fa415124b1935cd6e385a7fb4c727ac070cd5669</div>
      </div>
      <div class=cell>
        <h3>Ledger</h3>
        <p>Append-only. Every cent and every token, attributed at the model client, not self-reported.</p>
        <div class="vis mini ledgerlines">
          <div><span>budget</span><span>+$25.00</span></div>
          <div><span>task-1 tokens</span><span class=neg>&minus;$0.18</span></div>
          <div><span>task-2 tokens</span><span class=neg>&minus;$0.12</span></div>
          <div><span>task-3</span><span>refused</span></div>
        </div>
      </div>
      <div class=cell>
        <h3>Your keys</h3>
        <p>Bring your own Anthropic or OpenAI key. Encrypted at rest, used only for your missions. We never hold funds.</p>
        <div class="vis chips">
          <span class=chip><i></i>Anthropic</span><span class=chip><i></i>OpenAI</span>
          <span class=chip><i></i>0 funds custodied</span>
        </div>
      </div>
    </div>
  </div>
</div>

<section class=band id=live><div class=shell>
  <h2 class=big>One charter, one ledger, <span class=fade>every decision.</span></h2>
  <p class=sub>This is a live run against the hosted control plane, executed when you opened the page.
    Two of its decisions are refusals &mdash; that is the product working.</p>
  <div class=stage>
    <div class=win>
      <div class=bar><span class=lights aria-hidden=true><i></i><i></i><i></i></span>Activity<span class=sp></span>
        <span class="tag recorded" id=runTag>demo mission</span></div>
      <div class=stats>
        <div class="stat first"><div class=l>Decisions</div><div class=n id=stDecisions>&ndash;</div><div class=d id=stDecisionsD>gated against the charter</div></div>
        <div class=stat><div class=l>Refused</div><div class=n id=stRefused>&ndash;</div><div class=d>before any token was spent</div></div>
        <div class=stat><div class=l>Spend</div><div class=n id=stSpend>&ndash;</div><div class=d id=stSpendD>of the daily cap</div></div>
      </div>
      <div class=loghead><b>Decision log</b><span id=logCount>loading the live run&hellip;</span></div>
      <div class=tablewrap>
        <table class=log><thead><tr><th>Stage</th><th>Decision</th><th>What happened</th><th class=num>Amount</th></tr></thead>
          <tbody id=demoOut><tr class=skel><td class=stg>&mdash;</td><td></td><td>Fetching the run&hellip;</td><td></td></tr></tbody></table>
      </div>
      <div class=disc id=demoDisc>Governance decisions are real; no model is called in the demo, so task output is a placeholder.</div>
    </div>
    <aside class=receipt aria-label="Ledger receipt for the live run">
      <div class=hd><b><svg viewBox="0 0 24 24" aria-hidden=true><rect x="2" y="2" width="20" height="5" rx="1.5" fill="#121417"/><rect x="9.25" y="9.5" width="5.5" height="5" rx="1.2" fill="#0c5c46"/><rect x="2" y="17" width="20" height="5" rx="1.5" fill="none" stroke="#121417" stroke-width="1.5"/></svg>Ledger</b><span class=small id=rcDate></span></div>
      <div class=who>Demo workspace &middot; charter v1</div>
      <div id=rcLines><div class=ln><span>Waiting for the run</span><span>&hellip;</span></div></div>
      <div class=tot><div><div class=small>Total spent</div><strong id=rcTotal>&ndash;</strong><div class=small id=rcBal></div></div><div class=stamp>AUDITED</div></div>
      <svg class=barcode id=rcBarcode viewBox="0 0 256 38" preserveAspectRatio=none aria-hidden=true></svg>
      <div class=foot id=rcFoot>proof hash</div>
    </aside>
  </div>
</div></section>

<section class=measured><div class=shell>
  <div class=kicker>Measured, not claimed</div>
  <div class=facts>
    <div class=fact><div class=n>49</div><p>real model runs in the cost-calibration study</p></div>
    <div class=fact><div class=n>94<em>%</em></div><p>of pre-flight cost estimates land within 2&times; of the metered spend</p></div>
    <div class=fact><div class=n>14</div><p>threat-model attacks exercised against the running system</p></div>
    <div class=fact><div class=n>5</div><p>of them were real holes &mdash; found, fixed, and pinned by tests</p></div>
  </div>
  <p class=src>From the paper, <a href="https://arxiv.org/abs/2603.14011">arXiv 2603.14011</a>, which also reports where our own evaluation caught us overclaiming.</p>
</div></section>

<section class=pricing id=pricing><div class=shell>
  <h2 class=big>Start free. <span class=fade>Pay when it earns.</span></h2>
  <p class=sub>Model spend is on your own key, so the plan only buys governance capacity.</p>
  <div class=plans id=plans></div>
</div></section>

<section class=start id=start><div class=shell>
  <h2 class=big>Open a <em>workspace</em>.</h2>
  <p class=sub>Your key is shown once and stored hashed. Lose it and you rotate; we cannot read it back.</p>
  <div class=cols>
    <div class=card id=signupCard>
      <h4>Create workspace</h4><p class=lead>Thirty seconds, no card.</p>
      <label for=suName>Workspace name</label><input id=suName placeholder="Acme Inc." autocomplete=organization>
      <label for=suPlan>Plan</label><select id=suPlan></select>
      <div class=row><button class="btn dark" id=btn_signup type=button>Create workspace</button><span class=msg id=suMsg></span></div>
      <div id=keyBox class=hide>
        <p class=mut>Save this key now &mdash; it is shown once.</p>
        <div class=key id=apiKey></div>
      </div>
    </div>
    <div class=card>
      <h4>Sign in</h4><p class=lead>Paste your workspace key.</p>
      <label for=inKey>Workspace key</label><input id=inKey placeholder="sk_ten_&hellip;" autocomplete=off spellcheck=false>
      <div class=row><button class="btn dark" id=btn_signin type=button>Sign in</button><span class=msg id=siMsg></span></div>
    </div>
  </div>

  <div id=app class=hide>
    <div class="card wide">
      <h4>Workspace</h4>
      <div class=row id=meRow></div>
      <div class="row hide" id=govBox></div>
    </div>
    <div class=card>
      <h4>Bring your own keys</h4><p class=lead>Encrypted at rest; used only for your missions.</p>
      <label for=cfgAnthropic>Anthropic API key</label><input id=cfgAnthropic placeholder="sk-ant-&hellip;" autocomplete=off>
      <label for=cfgOpenAI>OpenAI API key</label><input id=cfgOpenAI placeholder="sk-&hellip;" autocomplete=off>
      <label for=cfgCharter>Charter YAML</label><textarea id=cfgCharter placeholder="mission: Ship weekly research briefs.&#10;fiscal_boundaries:&#10;  daily_burn_max_usd: 5.0"></textarea>
      <div class=row><button class="btn pine" id=btn_saveConfig type=button>Save</button><span class=msg id=cfgMsg></span></div>
    </div>
    <div class=card>
      <h4>Run a mission</h4><p class=lead>Planned, priced, gated, executed, audited.</p>
      <label for=goal>Goal</label><textarea id=goal placeholder="Summarize this week's agent-infrastructure releases."></textarea>
      <div class=row><button class="btn pine" id=btn_run type=button>Run</button><span class=msg id=runMsg></span></div>
      <div id=runOut class="out hide"></div>
    </div>
    <div class="card danger wide">
      <h4>Workspace control</h4>
      <p class=mut>Your workspace key is the only credential and is stored hashed &mdash; if it leaks, rotate: the old key stops working immediately.</p>
      <div class=row><button class="btn light" id=btn_rotate type=button>Rotate workspace key</button><span class=msg id=rotMsg></span></div>
      <div id=rotBox class=hide><div class=key id=rotKey></div></div>
      <p class=mut>Deleting removes this workspace, its stored provider keys, its ledger and its audit trail. It cannot be undone.</p>
      <div class=row><button class="btn warn" id=btn_delete type=button>Delete workspace</button><span class=msg id=delMsg></span></div>
    </div>
  </div>
</div></section>
</main>

<footer><div class=shell>
  <span>Sovereign-OS &middot; charter-governed agents</span>
  <nav aria-label=Footer><a href="https://github.com/Justin0504/Sovereign-OS">GitHub</a><a href="https://arxiv.org/abs/2603.14011">Paper</a><a href="https://sovereign-os-691.pages.dev">About</a></nav>
</div></footer>

<script nonce=__CSP_NONCE__>
var KEY=localStorage.getItem("sov_key")||"";
var CURRENT_ID="";
var SVG="http://www.w3.org/2000/svg";
function h(){return KEY?{"X-Tenant-Key":KEY,"Content-Type":"application/json"}:{"Content-Type":"application/json"}}
function el(id){return document.getElementById(id)}
function mk(tag,cls,text){var e=document.createElement(tag);if(cls)e.className=cls;if(text!=null)e.textContent=text;return e}
function cents(c){return c<100?(c+"¢"):("$"+(c%100?(c/100).toFixed(2):(c/100).toLocaleString()))}
var FEAT={governance:"Charter governance and CFO gate",audit:"Judge audit with proof hashes",dashboard:"Live decision dashboard",backends:"Pluggable execution backends",mcp:"MCP tool connectors",earning:"Paid job intake and settlement"};
function setMsg(id,cls,text){var m=el(id);m.className="msg"+(cls?" "+cls:"");m.textContent=text}

/* ---------- plans: pricing grid + signup select, one source ---------- */
fetch("/saas/plans").then(function(r){return r.json()}).then(function(d){
  var sel=el("suPlan"),grid=el("plans");sel.textContent="";grid.textContent="";
  (d.plans||[]).forEach(function(p,i){
    var o=mk("option",null,p.name+" — $"+(p.price_cents_month/100).toFixed(0)+"/mo · "+p.max_missions_per_day+" missions/day");
    o.value=p.name;sel.appendChild(o);
    var c=mk("div","plan"+(i===1?" hot":""));
    c.appendChild(mk("div","nm",p.name));
    var pr=mk("div","pr","$"+(p.price_cents_month/100).toFixed(0));pr.appendChild(mk("small",null," /month"));c.appendChild(pr);
    var ul=mk("ul");
    [p.max_missions_per_day.toLocaleString()+" governed missions a day",
     cents(p.max_daily_spend_cents)+" daily spend ceiling",
     p.seats+(p.seats===1?" seat":" seats")].concat((p.features||[]).map(function(f){return FEAT[f]||f})).forEach(function(t){ul.appendChild(mk("li",null,t))});
    c.appendChild(ul);
    var b=mk("button","btn "+(i===1?"dark":"light"),i===0?"Start free":"Choose "+p.name);b.type="button";
    b.addEventListener("click",function(){sel.value=p.name;el("start").scrollIntoView();el("suName").focus()});
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
  el("demoDisc").textContent=d.disclaimer||"";
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
    el("demoHint").textContent=d.refusals+" of "+(d.steps||[]).length+" decisions were refusals. Scroll down for the log and the receipt.";
    btn.disabled=false;
    if(scroll)el("live").scrollIntoView();
  }).catch(function(e){el("demoHint").textContent="The run did not complete: "+e;btn.disabled=false});
}
function copyCmd(){
  var t=el("cmdText").textContent,b=el("btn_copy");
  var done=function(){b.textContent="Copied";setTimeout(function(){b.textContent="Copy"},1600)};
  if(navigator.clipboard)navigator.clipboard.writeText(t).then(done,function(){b.textContent="Select & copy"});
}

/* ---------- workspace ---------- */
function signup(){
  setMsg("suMsg","","Creating…");
  fetch("/saas/tenants",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({name:el("suName").value,plan:el("suPlan").value})})
   .then(function(r){return r.json().then(function(d){return {ok:r.ok,d:d}})}).then(function(x){
     if(!x.ok){setMsg("suMsg","err",x.d.detail||"Error");return}
     el("keyBox").classList.remove("hide");el("apiKey").textContent=x.d.api_key;
     el("inKey").value=x.d.api_key;setMsg("suMsg","ok","Created. Copy your key, then sign in.");
   }).catch(function(e){setMsg("suMsg","err",String(e))})
}
function signin(){
  KEY=el("inKey").value.trim();
  fetch("/saas/tenants/me",{headers:h()}).then(function(r){return r.json().then(function(d){return {ok:r.ok,d:d}})}).then(function(x){
    if(!x.ok){setMsg("siMsg","err",x.d.detail||"Invalid key");return}
    localStorage.setItem("sov_key",KEY);el("app").classList.remove("hide");setMsg("siMsg","ok","Signed in.");renderMe(x.d);
  }).catch(function(e){setMsg("siMsg","err",String(e))})
}
function pill(text,dot){
  // textContent, not innerHTML: the workspace name is whatever the tenant typed at
  // signup, and the tenant API key sits in localStorage one DOM sink away.
  var s=mk("span","pill");if(dot)s.appendChild(mk("span","dot"));
  s.appendChild(document.createTextNode(text));return s;
}
function renderMe(d){
  var c=d.tenant.config||{},u=d.usage||{},row=el("meRow");
  CURRENT_ID=d.tenant.id||"";
  var ta=el("cfgCharter");if(document.activeElement!==ta){ta.value=c.charter_yaml||""}
  renderGovernance(d.governance||{});
  row.textContent="";
  [[d.tenant.name,1],["plan: "+d.tenant.plan,0],
   ["missions today: "+(u.missions||0)+"/"+d.plan.max_missions_per_day,0],
   ["spend today: $"+((u.spend_cents||0)/100).toFixed(2),0],
   ["anthropic: "+(c.anthropic_api_key||"—"),0],["openai: "+(c.openai_api_key||"—"),0]
  ].forEach(function(p){row.appendChild(pill(p[0],p[1]))});
}
function renderGovernance(g){
  var box=el("govBox");box.textContent="";
  if(!g.source){box.classList.add("hide");return}
  box.classList.remove("hide");
  if(g.source==="error"){
    box.appendChild(pill("charter will not load — missions refused",0));
    box.appendChild(mk("div","msg err",g.error));return;
  }
  box.appendChild(pill("limits from: "+(g.source==="charter"?"your charter":"default"),1));
  box.appendChild(pill("daily burn cap: "+(g.daily_burn_max_usd?("$"+g.daily_burn_max_usd.toFixed(2)):"none"),0));
  box.appendChild(pill("budget ceiling: "+(g.max_budget_usd?("$"+g.max_budget_usd.toFixed(2)):"none"),0));
  box.appendChild(pill("margin floor: "+Math.round((g.min_job_margin_ratio||0)*100)+"%",0));
}
function refreshMe(){fetch("/saas/tenants/me",{headers:h()}).then(function(r){return r.json()}).then(renderMe)}
function saveConfig(){
  setMsg("cfgMsg","","Saving…");
  var body={};if(el("cfgAnthropic").value)body.anthropic_api_key=el("cfgAnthropic").value;
  if(el("cfgOpenAI").value)body.openai_api_key=el("cfgOpenAI").value;
  if(el("cfgCharter").value)body.charter_yaml=el("cfgCharter").value;
  fetch("/saas/tenants/me/config",{method:"PUT",headers:h(),body:JSON.stringify(body)})
   .then(function(r){return r.json().then(function(d){return {ok:r.ok,d:d}})}).then(function(x){
     if(!x.ok){setMsg("cfgMsg","err",x.d.detail||"Error");return}
     setMsg("cfgMsg","ok","Saved.");el("cfgAnthropic").value="";el("cfgOpenAI").value="";refreshMe();
   }).catch(function(e){setMsg("cfgMsg","err",String(e))})
}
function run(){
  setMsg("runMsg","","Running…");el("runOut").classList.add("hide");
  fetch("/saas/tenants/me/missions",{method:"POST",headers:h(),body:JSON.stringify({goal:el("goal").value})})
   .then(function(r){return r.json().then(function(d){return {ok:r.ok,d:d}})}).then(function(x){
     if(!x.ok){setMsg("runMsg","err",x.d.detail||"Error");return}
     var d=x.d;setMsg("runMsg","ok",d.all_passed?"Completed · audit passed":"Completed · audit flagged issues");
     var o=(d.tasks||[]).map(function(t){return "● "+t.task_id+(t.success?" ✓":" ✗")+"\n"+(t.output||"")}).join("\n\n");
     el("runOut").textContent=o||"(no output)";el("runOut").classList.remove("hide");refreshMe();
   }).catch(function(e){setMsg("runMsg","err",String(e))})
}
function rotate(){
  if(!confirm("Rotate the workspace key? The current key stops working immediately."))return;
  setMsg("rotMsg","","Rotating…");
  fetch("/saas/tenants/me/rotate-key",{method:"POST",headers:h()})
   .then(function(r){return r.json().then(function(d){return {ok:r.ok,d:d}})}).then(function(x){
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
   .then(function(r){return r.json().then(function(d){return {ok:r.ok,d:d}})}).then(function(x){
     if(!x.ok){setMsg("delMsg","err",x.d.detail||"Error");return}
     localStorage.removeItem("sov_key");KEY="";el("app").classList.add("hide");
     setMsg("siMsg","ok","Workspace "+x.d.deleted+" deleted.");
   }).catch(function(e){setMsg("delMsg","err",String(e))})
}
el("btn_demo").addEventListener("click",function(){runDemo(true)});
el("btn_copy").addEventListener("click",copyCmd);
el("btn_rotate").addEventListener("click",rotate);
el("btn_delete").addEventListener("click",destroy);
el("btn_signup").addEventListener("click",signup);
el("btn_signin").addEventListener("click",signin);
el("btn_saveConfig").addEventListener("click",saveConfig);
el("btn_run").addEventListener("click",run);
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
