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
import os
from typing import Any

from sovereign_os.saas.plans import PLANS, get_plan
from sovereign_os.saas.runtime import run_tenant_mission, tenant_earning_active
from sovereign_os.saas.tenancy import TenantConfig, TenantStore

_STORE: TenantStore | None = None


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


def create_saas_app(store: TenantStore | None = None) -> Any:
    try:
        from fastapi import Body, Depends, FastAPI, Header, HTTPException
        from fastapi.responses import HTMLResponse
    except ImportError:  # pragma: no cover
        raise ImportError("fastapi required; pip install fastapi uvicorn")

    st = store or get_store()
    app = FastAPI(title="Sovereign-OS SaaS")

    def require_tenant(x_tenant_key: str | None = Header(default=None)):
        t = st.by_api_key(x_tenant_key or "")
        if t is None:
            raise HTTPException(status_code=401, detail="Invalid or missing X-Tenant-Key.")
        return t

    def _plan_info(plan_name: str) -> dict:
        p = get_plan(plan_name)
        return {"name": p.name, "price_cents_month": p.price_cents_month, "seats": p.seats,
                "max_missions_per_day": p.max_missions_per_day,
                "max_daily_spend_cents": p.max_daily_spend_cents, "features": sorted(p.features)}

    @app.get("/saas/health")
    def health():
        return {"status": "ok", "tenants": len(st.list())}

    @app.get("/saas/plans")
    def plans():
        return {"plans": [_plan_info(n) for n in PLANS]}

    @app.post("/saas/tenants")
    def signup(payload: dict | None = Body(None)):
        body = payload or {}
        name = str(body.get("name") or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="name is required.")
        t = st.create(name, plan=str(body.get("plan") or "free"))
        # api_key is returned ONCE at signup; it's never returned again.
        return {"tenant": t.public(), "api_key": t.api_key}

    @app.get("/saas/tenants/me")
    def me(t=Depends(require_tenant)):
        return {"tenant": t.public(), "plan": _plan_info(t.plan),
                "usage": dataclasses.asdict(st.usage(t.id)),
                "earning_active": tenant_earning_active(t)}

    @app.put("/saas/tenants/me/config")
    def update_config(payload: dict | None = Body(None), t=Depends(require_tenant)):
        body = payload or {}
        cfg = t.config
        for k in _CONFIG_FIELDS:
            if k in body:
                if k == "earning_enabled":
                    setattr(cfg, k, bool(body[k]))
                else:
                    setattr(cfg, k, str(body[k] or ""))
        if "plan" in body:  # (billing is out of scope for the MVP; allow set for now)
            t.plan = get_plan(body["plan"]).name
        st.update(t)
        return {"tenant": t.public(), "plan": _plan_info(t.plan)}

    @app.post("/saas/tenants/me/missions")
    async def run_mission(payload: dict | None = Body(None), t=Depends(require_tenant)):
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
    def console():
        return HTMLResponse(_CONSOLE_HTML)

    return app


# --------------------------------------------------------------------- console UI
_CONSOLE_HTML = """<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1">
<title>Sovereign-OS · Console</title>
<link rel=preconnect href=https://fonts.googleapis.com>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel=stylesheet>
<style>
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
  <div class=row><button class=btn onclick=signup()>Create workspace</button></div>
  <div class="msg" id=suMsg></div>
  <div id=keyBox class=hide style=margin-top:14px>
    <div class=mut>Save this API key — it is shown only once:</div>
    <div class=key id=apiKey></div>
  </div>
</div>

<div class=card>
  <h2>2 · Sign in</h2>
  <label>API key</label><input id=inKey placeholder="sk_ten_…">
  <div class=row><button class=btn onclick=signin()>Sign in</button><span class=msg id=siMsg></span></div>
</div>

<div id=app class=hide>
  <div class=card>
    <h2>Workspace</h2>
    <div class=row id=meRow></div>
  </div>
  <div class=card>
    <h2>3 · Bring your own keys</h2>
    <label>Anthropic API key</label><input id=cfgAnthropic placeholder="sk-ant-…">
    <label>OpenAI API key</label><input id=cfgOpenAI placeholder="sk-…">
    <label>Charter YAML (optional)</label><textarea id=cfgCharter placeholder="mission: ..."></textarea>
    <div class=row><button class=btn onclick=saveConfig()>Save</button><span class=msg id=cfgMsg></span></div>
    <div class=mut style=margin-top:8px>Keys are stored for your workspace only and used to run your missions. The platform never holds funds.</div>
  </div>
  <div class=card>
    <h2>4 · Run a mission</h2>
    <label>Goal</label><textarea id=goal placeholder="Summarize the market in one paragraph."></textarea>
    <div class=row><button class=btn onclick=run()>Run</button><span class=msg id=runMsg></span></div>
    <div id=runOut class="out hide"></div>
  </div>
</div>

<script>
var KEY=localStorage.getItem("sov_key")||"";
function h(){return KEY?{"X-Tenant-Key":KEY,"Content-Type":"application/json"}:{"Content-Type":"application/json"}}
function el(id){return document.getElementById(id)}
fetch("/saas/plans").then(r=>r.json()).then(d=>{el("suPlan").innerHTML=(d.plans||[]).map(p=>`<option value="${p.name}">${p.name} — $${(p.price_cents_month/100).toFixed(0)}/mo · ${p.max_missions_per_day} missions/day</option>`).join("")});
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
    if(d.tenant.config){el("cfgCharter").value="";}
  }).catch(e=>{el("siMsg").className="msg err";el("siMsg").textContent=String(e)})
}
function renderMe(d){
  var c=d.tenant.config||{},u=d.usage||{};
  el("meRow").innerHTML=`<span class=pill><span class=dot></span>${d.tenant.name}</span>`
   +`<span class=pill>plan: ${d.tenant.plan}</span>`
   +`<span class=pill>missions today: ${u.missions||0}/${d.plan.max_missions_per_day}</span>`
   +`<span class=pill>spend today: $${((u.spend_cents||0)/100).toFixed(2)}</span>`
   +`<span class=pill>anthropic: ${c.anthropic_api_key||"—"}</span>`
   +`<span class=pill>openai: ${c.openai_api_key||"—"}</span>`;
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
if(KEY){el("inKey").value=KEY;signin()}
</script></div></body></html>"""


def main() -> int:  # pragma: no cover
    import uvicorn

    uvicorn.run(create_saas_app(), host="0.0.0.0", port=int(os.getenv("PORT", "8020")))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
