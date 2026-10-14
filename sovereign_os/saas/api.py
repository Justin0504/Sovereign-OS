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
import logging
import os
import re
import secrets as _secrets
from typing import Any

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
        from fastapi.responses import HTMLResponse
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
        try:
            plan, results, reports = await run_tenant_mission(t, st, goal, max_repair_attempts=repair)
        except PermissionError as e:
            raise HTTPException(status_code=402, detail=str(e))  # limit / no key
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=f"mission failed: {e}")
        return {
            "goal": goal,
            "all_passed": all(getattr(r, "passed", False) for r in reports) if reports else True,
            "tasks": [{"task_id": r.task_id, "success": r.success, "output": (r.output or "")[:8000]}
                      for r in (results or [])],
            "audits": [{"task_id": rep.task_id, "passed": rep.passed, "score": rep.score,
                        "reason": rep.reason} for rep in (reports or [])],
            "usage": dataclasses.asdict(st.usage(t.id)),
        }

    @app.get("/", response_class=HTMLResponse)
    def console(request: Request):
        nonce = getattr(request.state, "csp_nonce", "")
        return HTMLResponse(_CONSOLE_HTML.replace("__CSP_NONCE__", nonce))

    return app


# --------------------------------------------------------------------- console UI
_CONSOLE_HTML = """<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1">
<title>Sovereign-OS · Console</title>
<link rel=preconnect href=https://fonts.googleapis.com>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel=stylesheet>
<style nonce=__CSP_NONCE__>
:root{--bg:#fff;--ink:#0d0d0d;--soft:#565869;--mut:#8e8ea0;--bd:#ececee;--elev:#f7f7f8;--ok:#10a37f;--bad:#e02e2a}
*{box-sizing:border-box}body{margin:0;font-family:Inter,-apple-system,sans-serif;background:var(--bg);color:var(--ink);font-size:15px;line-height:1.5}
.wrap{max-width:760px;margin:0 auto;padding:40px 22px 80px}
h1{font-size:1.7rem;font-weight:800;letter-spacing:-.02em;margin:0 0 4px}
.sub{color:var(--soft);margin:0 0 28px}
.card{border:1px solid var(--bd);border-radius:16px;padding:22px;margin-bottom:18px}
.card h2{font-size:.78rem;text-transform:uppercase;letter-spacing:.06em;color:var(--mut);margin:0 0 14px;font-weight:700}
label{display:block;font-size:.82rem;color:var(--soft);margin:12px 0 5px;font-weight:500}
input,textarea,select{width:100%;padding:10px 12px;border:1px solid var(--bd);border-radius:10px;font:inherit;background:#fff;color:var(--ink)}
input:focus,textarea:focus,select:focus{outline:2px solid var(--ink);outline-offset:1px;border-color:var(--ink)}
textarea{resize:vertical;min-height:70px}
.btn{padding:10px 18px;border:none;border-radius:10px;background:var(--ink);color:#fff;font:inherit;font-weight:600;cursor:pointer;transition:background .15s}
.btn:hover{background:#2b2b2b}.btn.ghost{background:var(--elev);color:var(--ink);border:1px solid var(--bd)}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-top:14px}
.key{font-family:ui-monospace,monospace;font-size:.82rem;background:var(--elev);border:1px solid var(--bd);border-radius:8px;padding:8px 10px;word-break:break-all}
.pill{display:inline-flex;gap:6px;align-items:center;font-size:.75rem;border:1px solid var(--bd);border-radius:999px;padding:3px 10px;color:var(--soft)}
.dot{width:7px;height:7px;border-radius:50%;background:var(--ok)}
.msg{font-size:.82rem;margin-top:10px;min-height:1em}.msg.err{color:var(--bad)}.msg.ok{color:var(--ok)}
.hide{display:none}.out{white-space:pre-wrap;background:var(--elev);border:1px solid var(--bd);border-radius:10px;padding:12px;font-size:.86rem;margin-top:12px;max-height:340px;overflow:auto}
.mut{color:var(--mut);font-size:.8rem}
</style></head><body><div class=wrap>
<h1>Sovereign-OS Console</h1>
<p class=sub>Governed AI agent workspace · bring your own keys · your work stays yours</p>

<div class=card id=signupCard>
  <h2>1 · Create workspace</h2>
  <label>Workspace name</label><input id=suName placeholder="Acme Inc.">
  <label>Plan</label><select id=suPlan></select>
  <div class=row><button class=btn id=btn_signup>Create workspace</button></div>
  <div class="msg" id=suMsg></div>
  <div id=keyBox class=hide style=margin-top:14px>
    <div class=mut>Save this API key — it is shown only once:</div>
    <div class=key id=apiKey></div>
  </div>
</div>

<div class=card>
  <h2>2 · Sign in</h2>
  <label>API key</label><input id=inKey placeholder="sk_ten_…">
  <div class=row><button class=btn id=btn_signin>Sign in</button><span class=msg id=siMsg></span></div>
</div>

<div id=app class=hide>
  <div class=card>
    <h2>Workspace</h2>
    <div class=row id=meRow></div>
    <div class="row hide" id=govBox></div>
  </div>
  <div class=card>
    <h2>3 · Bring your own keys</h2>
    <label>Anthropic API key</label><input id=cfgAnthropic placeholder="sk-ant-…">
    <label>OpenAI API key</label><input id=cfgOpenAI placeholder="sk-…">
    <label>Charter YAML (optional)</label><textarea id=cfgCharter placeholder="mission: ..."></textarea>
    <div class=row><button class=btn id=btn_saveConfig>Save</button><span class=msg id=cfgMsg></span></div>
    <div class=mut style=margin-top:8px>Keys are stored for your workspace only and used to run your missions. The platform never holds funds.</div>
  </div>
  <div class=card>
    <h2>4 · Run a mission</h2>
    <label>Goal</label><textarea id=goal placeholder="Summarize the market in one paragraph."></textarea>
    <div class=row><button class=btn id=btn_run>Run</button><span class=msg id=runMsg></span></div>
    <div id=runOut class="out hide"></div>
  </div>
</div>

<script nonce=__CSP_NONCE__>
var KEY=localStorage.getItem("sov_key")||"";
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
