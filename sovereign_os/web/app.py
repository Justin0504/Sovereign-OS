"""
Web UI: FastAPI dashboard — balance, tasks, decision stream, run mission.
"""

import asyncio
import logging
import os
from collections import deque
from dataclasses import dataclass, asdict
import time
from pathlib import Path
from threading import Thread
from typing import Any

logger = logging.getLogger(__name__)

# In-memory state shared with engine callbacks
_tasks: list[dict[str, Any]] = []
_logs: deque[tuple[str, str]] = deque(maxlen=500)
_engine: Any = None
_ledger: Any = None
_auth: Any = None
_charter_name: str = "Default"
_payment_service: Any = None
_job_store: Any = None  # sovereign_os.jobs.store.JobStore when SOVEREIGN_JOB_DB set


# ---------------------------------------------------------------------------
# Job queue (24/7 ingestion + human approval)
# ---------------------------------------------------------------------------


@dataclass
class Job:
    job_id: int
    goal: str
    charter: str
    amount_cents: int = 0
    currency: str = "USD"
    status: str = "pending"  # pending -> approved -> running -> completed / failed / payment_failed
    created_ts: float = time.time()
    updated_ts: float = time.time()
    payment_id: str | None = None
    error: str | None = None


_jobs: list[Job] = []
_next_job_id: int = 1


def _on_event(event_type: str, data: dict[str, Any]) -> None:
    """Called by GovernanceEngine; updates _tasks and _logs for the web UI."""
    global _tasks
    if event_type == "plan_created":
        _tasks = [
            {"task_id": t.get("task_id", ""), "skill": t.get("required_skill", ""), "status": "pending"}
            for t in data.get("tasks", [])
        ]
        _logs.append(("ceo", f"Plan created: {len(_tasks)} tasks. Goal: {(data.get('goal') or '')[:80]}..."))
    elif event_type == "task_started":
        task_id = data.get("task_id", "")
        agent_id = data.get("agent_id", "")
        for t in _tasks:
            if t.get("task_id") == task_id:
                t["status"] = "running"
                break
        _logs.append(("cfo", f"Task {task_id} started by {agent_id}"))
    elif event_type == "task_finished":
        task_id = data.get("task_id", "")
        success = data.get("success", False)
        status = "passed" if success else "failed"
        for t in _tasks:
            if t.get("task_id") == task_id:
                t["status"] = status
                break
        _logs.append(("system", f"Task {task_id} finished (success={success})"))
    elif event_type == "task_audited":
        task_id = data.get("task_id", "")
        passed = data.get("passed", False)
        score = data.get("score", 0)
        reason = data.get("reason", "")
        if passed:
            _logs.append(("auditor_pass", f"Task [{task_id}] verified. Score: {score:.2f}."))
        else:
            _logs.append(("auditor_fail", f"Task [{task_id}] FAILED. Reason: {reason}"))


def _enqueue_job(goal: str, charter: str, amount_cents: int = 0, currency: str = "USD") -> Job:
    """Create a new job in pending status. Requires human approval before execution."""
    global _next_job_id, _jobs, _job_store
    amount_cents = max(0, int(amount_cents))
    currency = currency or "USD"
    if _job_store is not None:
        row = _job_store.add_job(goal, charter, amount_cents=amount_cents, currency=currency)
        job = Job(
            job_id=row.job_id,
            goal=row.goal,
            charter=row.charter,
            amount_cents=row.amount_cents,
            currency=row.currency,
            status=row.status,
            created_ts=row.created_ts,
            updated_ts=row.updated_ts,
            payment_id=row.payment_id,
            error=row.error,
        )
        _next_job_id = row.job_id + 1
    else:
        job = Job(
            job_id=_next_job_id,
            goal=goal,
            charter=charter,
            amount_cents=amount_cents,
            currency=currency,
        )
        _next_job_id += 1
    _jobs.append(job)
    _logs.append(("system", f"Job {job.job_id} created (pending approval)."))
    return job


def _run_one_job(job: Job) -> None:
    """
    Execute a single approved job: run mission with audit, then on full success
    charge via PaymentService and record income in UnifiedLedger.
    """
    global _engine, _ledger, _payment_service
    if _engine is None:
        job.status = "failed"
        job.error = "Engine not configured"
        job.updated_ts = time.time()
        _logs.append(("auditor_fail", f"Job {job.job_id} failed: {job.error}"))
        return

    job.status = "running"
    job.updated_ts = time.time()
    if _job_store is not None:
        _job_store.update_job(job.job_id, status="running")
    _logs.append(("ceo", f"Job {job.job_id} running: {job.goal[:80]}{'…' if len(job.goal) > 80 else ''}"))

    async def _run_mission_and_settle() -> None:
        plan, results, reports = await _engine.run_mission_with_audit(job.goal, abort_on_audit_failure=False)
        all_passed = all(getattr(r, "passed", False) for r in reports) if reports else True
        if not all_passed:
            job.status = "failed"
            job.error = "One or more tasks failed audit"
            job.updated_ts = time.time()
            if _job_store is not None:
                _job_store.update_job(job.job_id, status="failed", error=job.error)
            _logs.append(("auditor_fail", f"Job {job.job_id} failed: audit did not pass."))
            return
        if job.amount_cents <= 0:
            job.status = "completed"
            job.updated_ts = time.time()
            if _job_store is not None:
                _job_store.update_job(job.job_id, status="completed")
            _logs.append(("auditor_pass", f"Job {job.job_id} completed (no charge)."))
            return
        if _payment_service is None:
            job.status = "completed"
            job.updated_ts = time.time()
            if _job_store is not None:
                _job_store.update_job(job.job_id, status="completed")
            _logs.append(("system", f"Job {job.job_id} completed (no payment service; income not recorded)."))
            return
        try:
            pid = await _payment_service.charge(
                job.amount_cents,
                job.currency.lower() if job.currency else "usd",
                metadata={"job_id": str(job.job_id), "goal": job.goal[:200]},
            )
            job.payment_id = pid
            if _ledger is not None:
                _ledger.record_usd(
                    job.amount_cents,
                    purpose="job_income",
                    ref=f"job-{job.job_id}",
                )
            job.status = "completed"
            job.updated_ts = time.time()
            if _job_store is not None:
                _job_store.update_job(job.job_id, status="completed", payment_id=pid)
            _logs.append(("auditor_pass", f"Job {job.job_id} completed. Charged {job.amount_cents/100:.2f} {job.currency} (ref={pid})."))
        except Exception as e:
            job.status = "payment_failed"
            job.error = str(e)
            job.updated_ts = time.time()
            if _job_store is not None:
                _job_store.update_job(job.job_id, status="payment_failed", error=job.error)
            _logs.append(("auditor_fail", f"Job {job.job_id} payment failed: {e}"))
            logger.exception("Job %s payment failed", job.job_id)

    async def _run() -> None:
        try:
            await _run_mission_and_settle()
        except Exception as e:
            job.status = "failed"
            job.error = str(e)
            job.updated_ts = time.time()
            if _job_store is not None:
                _job_store.update_job(job.job_id, status="failed", error=job.error)
            _logs.append(("auditor_fail", f"Mission error for Job {job.job_id}: {e}"))
            logger.exception("Job %s mission failed", job.job_id)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_run())
    finally:
        loop.close()


def _job_worker() -> None:
    """Background thread: pick approved jobs one at a time and run them."""
    while True:
        time.sleep(5)
        for j in _jobs:
            if j.status == "approved":
                _run_one_job(j)
                break


_HTML_DASHBOARD = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Sovereign-OS · Command Center</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Poppins:wght@400;500;600;700&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg: #faf9f6;
      --bg-card: #ffffff;
      --text: #0a0a0a;
      --text-soft: #404040;
      --text-muted: #737373;
      --border: rgba(0,0,0,0.08);
      --black: #0a0a0a;
      --success: #166534;
      --warn: #854d0e;
      --danger: #991b1b;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: 'Poppins', -apple-system, BlinkMacSystemFont, sans-serif;
      background: var(--bg);
      color: var(--text);
      min-height: 100vh;
      font-size: 18px;
      line-height: 1.6;
      -webkit-font-smoothing: antialiased;
      text-align: left;
    }
    .topbar {
      background: var(--bg-card);
      border-bottom: 1px solid var(--border);
      padding: 20px 32px;
      display: flex;
      align-items: center;
      justify-content: space-between;
    }
    .topbar-brand {
      display: flex;
      align-items: center;
      gap: 8px;
      font-size: 1.25rem;
      font-weight: 700;
      color: var(--black);
      letter-spacing: -0.02em;
    }
    .logo-mark {
      width: 18px;
      height: 18px;
      border-radius: 999px;
      border: 1px solid var(--black);
      display: inline-flex;
      align-items: center;
      justify-content: center;
    }
    .logo-mark::before {
      content: "";
      width: 6px;
      height: 6px;
      border-radius: 999px;
      background: var(--black);
    }
    .topbar-meta {
      font-size: 0.9375rem;
      color: var(--text-muted);
    }
    .topbar-meta strong { color: var(--black); font-weight: 600; margin-left: 4px; }
    .wrap {
      max-width: 680px;
      margin: 0 auto;
      padding: 64px 32px 96px 32px;
      text-align: left;
    }
    .hero {
      margin-bottom: 64px;
      text-align: left;
    }
    .hero h1 {
      font-size: 2.75rem;
      font-weight: 700;
      letter-spacing: -0.03em;
      color: var(--black);
      line-height: 1.2;
      margin-bottom: 20px;
      text-align: left;
    }
    .hero p {
      font-size: 1.125rem;
      color: var(--text-soft);
      font-weight: 400;
      max-width: 520px;
      margin-top: 4px;
      margin-left: 0;
      text-align: left;
    }
    .strip {
      display: flex;
      gap: 28px;
      padding: 20px 0 32px;
      margin-bottom: 40px;
      border-bottom: 1px solid var(--border);
      font-size: 0.9375rem;
      color: var(--text-muted);
      justify-content: flex-start;
      text-align: left;
    }
    .strip strong { color: var(--black); font-weight: 600; }
    .prompt-box {
      margin-bottom: 56px;
    }
    .prompt-box label {
      display: block;
      font-size: 1.125rem;
      font-weight: 600;
      color: var(--black);
      margin-bottom: 16px;
      text-align: left;
    }
    .prompt-row {
      display: flex;
      gap: 14px;
      align-items: stretch;
      justify-content: flex-start;
    }
    .prompt-row input {
      flex: 1;
      padding: 20px 24px;
      font-size: 1.0625rem;
      font-family: inherit;
      background: var(--bg-card);
      border: 1px solid var(--border);
      border-radius: 12px;
      color: var(--black);
      transition: border-color 0.15s, box-shadow 0.15s;
    }
    .prompt-row input::placeholder { color: var(--text-muted); }
    .prompt-row input:focus {
      outline: none;
      border-color: var(--black);
      box-shadow: 0 0 0 2px rgba(0,0,0,0.06);
    }
    .btn {
      padding: 20px 36px;
      font-size: 1rem;
      font-weight: 600;
      font-family: inherit;
      background: var(--black);
      color: #fff;
      border: none;
      border-radius: 12px;
      cursor: pointer;
      transition: background 0.15s, box-shadow 0.15s, transform 0.15s;
    }
    .btn:hover {
      background: #262626;
      box-shadow: 0 8px 18px rgba(0,0,0,0.12);
      transform: translateY(-1px);
    }
    .section-title {
      font-size: 1.25rem;
      font-weight: 700;
      color: var(--black);
      margin-bottom: 4px;
      letter-spacing: -0.02em;
      text-align: left;
    }
    .section-subtitle {
      font-size: 0.9375rem;
      color: var(--text-muted);
      margin-bottom: 20px;
    }
    .feed {
      background: var(--bg-card);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 6px 0;
      max-height: 380px;
      overflow-y: auto;
    }
    .feed-item {
      padding: 18px 28px;
      font-size: 1rem;
      line-height: 1.6;
      border-bottom: 1px solid var(--border);
      color: var(--text-soft);
    }
    .feed-item:last-child { border-bottom: none; }
    .feed-item.ceo { color: var(--black); font-weight: 500; }
    .feed-item.cfo { color: var(--black); opacity: 0.9; }
    .feed-item.auditor_pass { color: var(--success); }
    .feed-item.auditor_fail { color: var(--danger); }
    .tasks-row {
      display: flex;
      flex-wrap: wrap;
      gap: 14px;
      margin-top: 16px;
      justify-content: flex-start;
    }
    .task-pill {
      display: inline-flex;
      align-items: center;
      gap: 10px;
      padding: 14px 20px;
      font-size: 1rem;
      background: var(--bg-card);
      border: 1px solid var(--border);
      border-radius: 12px;
      color: var(--text-soft);
      transition: background 0.12s, box-shadow 0.12s, border-color 0.12s;
    }
    .task-pill:hover {
      background: #f3f1eb;
      border-color: rgba(0,0,0,0.14);
      box-shadow: 0 4px 10px rgba(0,0,0,0.04);
    }
    .task-pill .dot {
      width: 10px; height: 10px; border-radius: 50%;
    }
    .task-pill.pending .dot { background: var(--text-muted); }
    .task-pill.running .dot { background: var(--black); }
    .task-pill.passed .dot { background: var(--success); }
    .task-pill.failed .dot { background: var(--danger); }
    .task-pill .id { font-weight: 600; color: var(--black); }
    .task-pill.job-completed { border-color: rgba(22, 101, 52, 0.4); color: var(--success); }
    .task-pill.job-failed, .task-pill.job-payment_failed { border-color: rgba(153, 27, 27, 0.4); color: var(--danger); }
    .task-pill.job-running { border-color: rgba(10, 10, 10, 0.3); }
    .stats {
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      gap: 32px;
      margin-top: 56px;
      padding-top: 40px;
      border-top: 1px solid var(--border);
      text-align: left;
    }
    @media (max-width: 640px) { .stats { grid-template-columns: repeat(2, 1fr); } }
    .stat .label { font-size: 0.9375rem; color: var(--text-muted); margin-bottom: 8px; }
    .stat .value { font-size: 1.5rem; font-weight: 700; color: var(--black); font-variant-numeric: tabular-nums; letter-spacing: -0.02em; }
    .empty-feed { padding: 32px 28px; color: var(--text-muted); font-size: 1rem; }
    .footer-strip {
      margin-top: 40px;
      padding-top: 20px;
      border-top: 1px solid var(--border);
      font-size: 0.875rem;
      color: var(--text-muted);
    }
  </style>
</head>
<body>
  <header class="topbar">
    <span class="topbar-brand"><span class="logo-mark"></span>Sovereign-OS</span>
    <span class="topbar-meta">Charter <strong id="charter">—</strong> · Balance <strong id="balance">—</strong> · Tokens <strong id="tokens">—</strong> · Trust <strong id="trust">—</strong></span>
  </header>
  <div class="wrap">
    <header class="hero">
      <h1>Command Center</h1>
      <p>Run a mission and watch the stream. Plan, approve, execute, audit.</p>
    </header>
    <div class="strip">
      <span>Charter <strong id="charter2">—</strong></span>
      <span>Balance <strong id="balance2">—</strong></span>
      <span>Tokens <strong id="tokens2">—</strong></span>
      <span>Trust <strong id="trust2">—</strong></span>
    </div>
    <section class="prompt-box">
      <label for="goal">Run a mission</label>
      <div class="prompt-row">
        <input type="text" id="goal" placeholder="Describe what the entity should do..." value="Summarize the market in one paragraph." />
        <button class="btn" onclick="runMission()">Run</button>
      </div>
    </section>
    <section>
      <div class="section-title">Job queue</div>
      <p class="section-subtitle">External orders can be ingested as jobs, approved by a human, and then executed 24/7.</p>
      <div class="tasks-row" id="jobs"></div>
    </section>
    <section>
      <div class="section-title">Tasks</div>
      <div class="tasks-row" id="tasks"></div>
    </section>
    <section style="margin-top: 48px;">
      <div class="section-title">Activity</div>
      <p class="section-subtitle">Every mission creates a verifiable audit trail of plans, spending, execution, and review.</p>
      <div class="feed" id="logs"></div>
    </section>
    <div class="stats">
      <div class="stat"><div class="label">Balance</div><div class="value" id="finBalance">$0.00</div></div>
      <div class="stat"><div class="label">Tokens burned</div><div class="value" id="finTokens">0</div></div>
      <div class="stat"><div class="label">Agent</div><div class="value" id="finAgent">—</div></div>
      <div class="stat"><div class="label">Trust score</div><div class="value" id="finTrust">—</div></div>
    </div>
    <div class="footer-strip">
      Sovereign‑OS is experimental software. Always keep a human in the loop for financial and production decisions.
    </div>
  </div>
  <script>
    function r(id) { return document.getElementById(id); }
    function fetchStatus() {
      fetch('/api/status').then(x => x.json()).then(d => {
        const charter = d.charter || '—', balance = d.balance || '—', tokens = d.tokens || '—', trust = d.trust_score || '—';
        r('charter').textContent = charter;
        r('balance').textContent = balance;
        r('tokens').textContent = tokens;
        r('trust').textContent = trust;
        if (r('charter2')) { r('charter2').textContent = charter; r('balance2').textContent = balance; r('tokens2').textContent = tokens; r('trust2').textContent = trust; }
        r('finBalance').textContent = balance === '—' ? '$0.00' : balance;
        r('finTokens').textContent = tokens === '—' ? '0' : tokens;
        r('finAgent').textContent = (d.agent_id || '—').slice(0, 20);
        r('finTrust').textContent = trust;
      }).catch(() => {});
    }
    function fetchTasks() {
      fetch('/api/tasks').then(x => x.json()).then(d => {
        const el = r('tasks');
        const tasks = d.tasks || [];
        el.innerHTML = tasks.length ? tasks.map(t =>
          '<div class="task-pill ' + t.status + '"><span class="dot"></span><span class="id">' + escapeHtml(t.task_id) + '</span><span>· ' + escapeHtml(t.skill || '') + '</span></div>'
        ).join('') : '<span class="task-pill" style="color:var(--text-muted)">No tasks yet. Launch a mission to see your DAG.</span>';
      }).catch(() => {});
    }
    function fetchJobs() {
      fetch('/api/jobs').then(x => x.json()).then(d => {
        const el = r('jobs');
        const jobs = d.jobs || [];
        if (!jobs.length) {
          el.innerHTML = '<span class="task-pill" style="color:var(--text-muted)">No external jobs. POST to /api/jobs to ingest work.</span>';
          return;
        }
        el.innerHTML = jobs.map(j => {
          const cls = 'task-pill job-' + j.status;
          const label = 'job-' + j.job_id;
          const amount = j.amount_cents ? ' · ' + (j.amount_cents/100).toFixed(2) + ' ' + (j.currency || 'USD') : '';
          const status = ' [' + j.status + ']';
          const approveBtn = j.status === 'pending'
            ? ' <button class="btn" style="padding:6px 10px;font-size:0.8rem" onclick="approveJob(' + j.job_id + ')">Approve</button>'
            : '';
          return '<div class=\"' + cls + '\"><span class=\"id\">' + label + '</span><span>' + amount + status + '</span>' + approveBtn + '</div>';
        }).join('');
      }).catch(() => {});
    }
    function fetchLogs() {
      fetch('/api/logs').then(x => x.json()).then(d => {
        const div = r('logs');
        const logs = (d.logs || []).slice(-50);
        div.innerHTML = logs.length ? logs.map(l => '<div class="feed-item ' + l.source + '">' + escapeHtml(l.message) + '</div>').join('') : '<div class="empty-feed">When your company thinks, this stream becomes your audit trail.</div>';
        div.scrollTop = div.scrollHeight;
      }).catch(() => {});
    }
    function escapeHtml(s) { const e = document.createElement('div'); e.textContent = s; return e.innerHTML; }
    function runMission() {
      const goal = r('goal').value || 'Summarize the market in one paragraph.';
      fetch('/api/run', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ goal: goal }) }).then(x => x.json()).then(d => { if (d.status === 'started') fetchLogs(); }).catch(() => {});
    }
    function approveJob(id) {
      fetch('/api/jobs/' + id + '/approve', { method: 'POST' }).then(() => { fetchJobs(); fetchLogs(); }).catch(() => {});
    }
    setInterval(fetchStatus, 2000);
    setInterval(fetchTasks, 1500);
    setInterval(fetchLogs, 1000);
    setInterval(fetchJobs, 5000);
    fetchStatus(); fetchTasks(); fetchLogs(); fetchJobs();
  </script>
</body>
</html>
"""


def _api_key_dependency():
    """Dependency: require X-API-Key or Authorization Bearer when SOVEREIGN_API_KEY is set."""
    from fastapi import Header, HTTPException
    key = os.getenv("SOVEREIGN_API_KEY")
    if not key:
        def _noop():
            return
        return _noop
    def _verify(x_api_key: str | None = Header(None), authorization: str | None = Header(None)):
        token = x_api_key or (authorization.split(" ", 1)[-1] if authorization and str(authorization).lower().startswith("bearer ") else None)
        if token != key:
            raise HTTPException(status_code=401, detail="Invalid or missing API key")
    return _verify


def create_app(
    engine: Any = None,
    ledger: Any = None,
    auth: Any = None,
    charter_name: str = "Default",
) -> Any:
    """Create FastAPI app with dashboard and API. Optionally inject engine/ledger/auth."""
    try:
        from fastapi import Body, Depends, FastAPI, Request
        from fastapi.responses import HTMLResponse, JSONResponse
    except ImportError:
        raise ImportError("fastapi required for web UI; pip install fastapi uvicorn")

    global _engine, _ledger, _auth, _charter_name
    _engine = engine
    _ledger = ledger
    _auth = auth
    _charter_name = charter_name
    if engine is not None:
        engine._on_event = _on_event

    app = FastAPI(title="Sovereign-OS Command Center (Web)")

    @app.get("/health")
    def health():
        """Health check for load balancers and orchestrators. Returns 200 if ledger is readable; 503 if critical failure."""
        try:
            ledger_ok = _ledger is not None
            if _ledger:
                _ledger.total_usd_cents()
            redis_ok = None
            redis_url = os.getenv("REDIS_URL")
            if redis_url:
                try:
                    import redis
                    r = redis.from_url(redis_url)
                    r.ping()
                    redis_ok = True
                except Exception:
                    redis_ok = False
            status = "ok" if ledger_ok else "degraded"
            body = {"status": status, "ledger": ledger_ok, "redis": redis_ok}
            return body if ledger_ok else (JSONResponse(status_code=503, content=body))
        except Exception as e:
            logger.exception("Health check failed")
            return JSONResponse(status_code=503, content={"status": "error", "error": str(e)})

    @app.get("/", response_class=HTMLResponse)
    def index():
        return _HTML_DASHBOARD

    @app.get("/api/status")
    def api_status():
        balance = "$0.00"
        tokens = "0"
        trust = "—"
        agent_id = "—"
        if _ledger:
            balance = f"${_ledger.total_usd_cents() / 100:.2f}"
            by_model = getattr(_ledger, "total_tokens_by_model", lambda: {})()
            tokens = f"{sum(by_model.values()) if isinstance(by_model, dict) else 0:,}"
        if _auth and getattr(_auth, "_scores", None):
            if _auth._scores:
                last = list(_auth._scores.keys())[-1]
                agent_id = last
                trust = str(_auth._scores[last])
            else:
                trust = str(getattr(_auth, "_base", 50))
        return {"balance": balance, "tokens": tokens, "trust_score": trust, "agent_id": agent_id, "charter": _charter_name}

    @app.get("/api/tasks")
    def api_tasks():
        return {"tasks": list(_tasks)}

    @app.get("/api/logs")
    def api_logs():
        return {"logs": [{"source": s, "message": m} for s, m in _logs]}

    @app.get("/api/jobs")
    def api_jobs():
        return {
            "jobs": [
                asdict(j)
                for j in sorted(_jobs, key=lambda x: x.job_id)
            ]
        }

    @app.post(
        "/api/jobs",
        summary="Create job (pending approval)",
        description="Submit a new job. Body: goal (str), charter (str, optional), amount_cents (int), currency (str). If SOVEREIGN_API_KEY is set, send X-API-Key or Authorization: Bearer <key>.",
    )
    def api_jobs_create(
        payload: dict | None = Body(None),
        _: None = Depends(_api_key_dependency()),
    ):
        body = payload or {}
        goal = str(body.get("goal") or "Summarize the market in one paragraph.").strip()
        charter = str(body.get("charter") or _charter_name or "Default")
        amount_cents = int(body.get("amount_cents") or 0)
        currency = str(body.get("currency") or "USD")
        job = _enqueue_job(goal, charter, amount_cents=amount_cents, currency=currency)
        return {"job": asdict(job)}

    @app.post("/api/jobs/{job_id}/approve")
    def api_jobs_approve(job_id: int):
        global _job_store
        for j in _jobs:
            if j.job_id == job_id:
                j.status = "approved"
                j.updated_ts = time.time()
                if _job_store is not None:
                    _job_store.update_job(job_id, status="approved")
                _logs.append(("ceo", f"Job {job_id} approved for execution."))
                return {"job": asdict(j)}
        return JSONResponse(status_code=404, content={"error": "Job not found"})

    @app.post("/api/run")
    def api_run(payload: dict | None = Body(None)):
        goal = (payload or {}).get("goal", "Summarize the market in one paragraph.") if isinstance(payload, dict) else "Summarize the market in one paragraph."
        goal = (goal or "Summarize the market in one paragraph.").strip() or "Summarize the market in one paragraph."
        if _engine is None:
            return JSONResponse(status_code=503, content={"error": "Engine not configured. Start the app with: python -m sovereign_os.web.app"})

        _logs.append(("ceo", f"Mission started: {goal[:100]}{'…' if len(goal) > 100 else ''}"))

        def run():
            async def _run():
                try:
                    await _engine.run_mission_with_audit(goal, abort_on_audit_failure=False)
                except Exception as e:
                    _logs.append(("auditor_fail", f"Mission error: {e}"))
                    logger.exception("Mission failed")

            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(_run())
            finally:
                loop.close()

        Thread(target=run, daemon=True).start()
        return {"status": "started", "goal": goal[:80]}

    @app.post("/api/webhooks/stripe")
    async def api_webhooks_stripe(request: Request):
        """Stripe webhook: verify signature (if STRIPE_WEBHOOK_SECRET set) and acknowledge."""
        payload = await request.body()
        secret = os.getenv("STRIPE_WEBHOOK_SECRET")
        if secret:
            try:
                import stripe
                sig = request.headers.get("Stripe-Signature", "")
                stripe.Webhook.construct_event(payload, sig, secret)
            except Exception as e:
                logger.warning("WEBHOOK: Stripe signature verification failed: %s", e)
                return JSONResponse(status_code=400, content={"error": "Invalid signature"})
        return {"received": True}

    return app


def run_web_ui(
    host: str = "0.0.0.0",
    port: int = 8000,
    charter_path: str | None = None,
) -> None:
    """Load charter, create engine/ledger/auth, then run FastAPI dashboard."""
    from sovereign_os import load_charter, UnifiedLedger
    from sovereign_os.agents import SovereignAuth
    from sovereign_os.auditor import ReviewEngine
    from sovereign_os.governance import GovernanceEngine
    from sovereign_os.payments.service import create_payment_service

    root = Path(__file__).resolve().parents[2]
    if charter_path and Path(charter_path).exists():
        path = charter_path
    elif (root / "charter.example.yaml").exists():
        path = str(root / "charter.example.yaml")
    elif (root / "charters" / "The_Freelancer.yaml").exists():
        path = str(root / "charters" / "The_Freelancer.yaml")
    else:
        raise FileNotFoundError(
            "No charter file found. Add charter.example.yaml in project root or run with --charter path/to/charter.yaml"
        )
    charter = load_charter(path)
    ledger_path = os.getenv("SOVEREIGN_LEDGER_PATH")
    ledger = UnifiedLedger(persist_path=ledger_path) if ledger_path else UnifiedLedger()
    if ledger.total_usd_cents() == 0:
        ledger.record_usd(1000)  # 1000 cents = $10.00 demo balance (seed when empty)
    auth = SovereignAuth()
    review = ReviewEngine(charter)
    engine = GovernanceEngine(charter, ledger, auth=auth, review_engine=review, on_event=_on_event)

    # Initialize payment service once per process
    global _payment_service
    try:
        _payment_service = create_payment_service()
    except Exception as e:  # pragma: no cover - optional
        logger.warning("PAYMENTS: Failed to initialize payment service: %s", e)
        _payment_service = None

    charter_name = Path(path).stem.replace("_", " ").title()
    job_db = os.getenv("SOVEREIGN_JOB_DB")
    if job_db:
        from sovereign_os.jobs.store import JobStore
        global _job_store, _jobs, _next_job_id
        _job_store = JobStore(job_db)
        _jobs.clear()
        for row in _job_store.list_jobs():
            _jobs.append(Job(
                job_id=row.job_id,
                goal=row.goal,
                charter=row.charter,
                amount_cents=row.amount_cents,
                currency=row.currency,
                status=row.status,
                created_ts=row.created_ts,
                updated_ts=row.updated_ts,
                payment_id=row.payment_id,
                error=row.error,
            ))
        _next_job_id = max((j.job_id for j in _jobs), default=0) + 1
        logger.info("Sovereign-OS: Job store loaded from %s (%s jobs)", job_db, len(_jobs))
    app = create_app(engine=engine, ledger=ledger, auth=auth, charter_name=charter_name)
    t = Thread(target=_job_worker, daemon=True)
    t.start()
    try:
        from sovereign_os.ingest.poller import start_ingest_poller
        if start_ingest_poller(_enqueue_job):
            logger.info("Sovereign-OS Web UI: ingest poller started (SOVEREIGN_INGEST_URL).")
    except Exception as e:
        logger.warning("INGEST: could not start poller: %s", e)
    logger.info("Sovereign-OS Web UI: job worker started (24/7). Open http://localhost:%s (or http://127.0.0.1:%s)", port, port)
    import uvicorn
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    import sys
    port = 8000
    charter_path = None
    for i, arg in enumerate(sys.argv[1:], 1):
        if arg.isdigit():
            port = int(arg)
        elif arg.endswith(".yaml") or arg.endswith(".yml"):
            charter_path = arg
    run_web_ui(port=port, charter_path=charter_path)
