"""
Web UI: FastAPI dashboard — balance, tasks, decision stream, run mission.
"""

# Load .env from project root and cwd so OPENAI_API_KEY / cost-control vars are applied (override=True so .env wins)
try:
    from dotenv import load_dotenv
    from pathlib import Path as _Path
    _root = _Path(__file__).resolve().parents[2]
    load_dotenv(_root / ".env", override=True)
    load_dotenv(_Path.cwd() / ".env", override=True)
except Exception:
    pass

import asyncio
import json
import logging
import os
import signal
import uuid
from collections import deque
from dataclasses import dataclass, asdict
import time
from datetime import datetime, timezone
from pathlib import Path
from threading import Thread
from typing import Any

# Graceful shutdown: when set, job worker stops picking new jobs after current one finishes
_shutdown_requested = False
_last_job_completed_at: float | None = None  # for /health
_job_concurrency_semaphore: Any = None  # threading.Semaphore when SOVEREIGN_JOB_WORKER_CONCURRENCY > 1

logger = logging.getLogger(__name__)


def _safe_int(value: Any, default: int = 0, min_val: int | None = None, max_val: int | None = None) -> int:
    """Coerce to int; use default on TypeError/ValueError. Optionally clamp to [min_val, max_val]."""
    try:
        n = int(float(value)) if value not in (None, "") else default
    except (TypeError, ValueError):
        n = default
    if min_val is not None and n < min_val:
        n = min_val
    if max_val is not None and n > max_val:
        n = max_val
    return n


def _safe_float(value: Any, default: float | None = 0.0) -> float | None:
    """Coerce to float; use default on TypeError/ValueError. Returns None if value is None and default is None."""
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default

# In-memory state shared with engine callbacks
_tasks: list[dict[str, Any]] = []
_logs: deque[tuple[str, str]] = deque(maxlen=500)
_engine: Any = None
_ledger: Any = None
_auth: Any = None
_charter_name: str = "Default"
_charter_path: str | None = None  # path to charter YAML (for GET/PUT /api/charter)
_payment_service: Any = None
_job_store: Any = None  # sovereign_os.jobs.store.JobStore when SOVEREIGN_JOB_DB set


def _ui_overrides_path() -> Path:
    """Path to UI overrides JSON (access + settings)."""
    root = Path(__file__).resolve().parent.parent.parent
    data_dir = Path(os.getenv("SOVEREIGN_DATA_DIR", str(root / "data")))
    return data_dir / "ui_overrides.json"


def _get_ui_overrides() -> dict[str, Any]:
    """Read UI overrides from file. Returns {} if missing or invalid."""
    path = _ui_overrides_path()
    if not path.exists():
        return {}
    try:
        import json
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _set_ui_overrides_section(section: str, updates: dict[str, Any]) -> None:
    """Merge updates into a section (e.g. 'access', 'settings') and write file."""
    import json
    path = _ui_overrides_path()
    current = _get_ui_overrides()
    if section not in current:
        current[section] = {}
    for key, value in updates.items():
        if value is None:
            current[section].pop(key, None)
        else:
            current[section][key] = value
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(current, indent=2), encoding="utf-8")


def _effective_auto_approve() -> bool:
    """True if jobs should be auto-approved (overlay then env)."""
    o = _get_ui_overrides()
    s = o.get("settings") or {}
    v = s.get("SOVEREIGN_AUTO_APPROVE_JOBS")
    if v is not None:
        return str(v).strip().lower() in ("1", "true", "yes")
    return (os.getenv("SOVEREIGN_AUTO_APPROVE_JOBS") or "").strip().lower() in ("1", "true", "yes")


def _effective_compliance_auto() -> bool:
    """True if compliance should auto-proceed (overlay then env)."""
    o = _get_ui_overrides()
    s = o.get("settings") or {}
    v = s.get("SOVEREIGN_COMPLIANCE_AUTO_PROCEED")
    if v is not None:
        return str(v).strip().lower() in ("1", "true", "yes")
    return (os.getenv("SOVEREIGN_COMPLIANCE_AUTO_PROCEED") or "").strip().lower() in ("1", "true", "yes")


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
    callback_url: str | None = None  # optional per-job webhook; overrides SOVEREIGN_WEBHOOK_URL when set
    retry_count: int = 0  # number of retries; used by POST /api/jobs/{id}/retry
    request_id: str | None = None  # trace id for logs and webhook
    priority: int = 0  # higher = run first when approved
    run_after_ts: float | None = None  # run only after this Unix timestamp (scheduling)
    delivery_contact: dict | None = None  # e.g. {"platform":"reddit","username":"x","post_id":"y"} for reply/DM after completion


_jobs: list[Job] = []
_next_job_id: int = 1
_job_results: dict[int, dict[str, Any]] = {}  # job_id -> {goal, tasks: [{task_id, skill, output, success}], combined_output}
_job_progress: dict[int, dict[str, Any]] = {}  # job_id -> {stage, tasks_total, tasks_done, current_task, pct}

# Built-in job templates
_JOB_TEMPLATES = [
    {"id": "blog_post", "name": "Blog Post", "goal": "Write a 500-word blog post about {topic}", "default_amount": 1500, "category": "Writing", "description": "Professional blog post with SEO-ready structure"},
    {"id": "research_brief", "name": "Research Brief", "goal": "Produce a research brief on {topic} with key findings and recommendations", "default_amount": 1800, "category": "Research", "description": "Executive-facing research with actionable insights"},
    {"id": "competitive_analysis", "name": "Competitive Analysis", "goal": "Create a one-page competitive snapshot of {topic}", "default_amount": 2000, "category": "Research", "description": "Comparison table with differentiators"},
    {"id": "cold_email", "name": "Cold Outreach Email", "goal": "Draft a cold outreach email to {target} offering {service}", "default_amount": 1000, "category": "Sales", "description": "3 subject line variants + body + CTA"},
    {"id": "social_post", "name": "Social Media Post", "goal": "Write a {platform} post announcing {topic}", "default_amount": 700, "category": "Marketing", "description": "5 hook variants + hashtags + engagement tips"},
    {"id": "translate", "name": "Translation", "goal": "Translate the following to {language}: {text}", "default_amount": 600, "category": "Translation", "description": "Accurate translation preserving formatting"},
    {"id": "meeting_minutes", "name": "Meeting Minutes", "goal": "Write meeting minutes for: {topic}", "default_amount": 900, "category": "Operations", "description": "Decisions, action items, and follow-ups"},
    {"id": "strategy_doc", "name": "Strategy Template", "goal": "Create a strategy document for {topic}", "default_amount": 2000, "category": "Strategy", "description": "Go-to-market plan with actionable steps"},
    {"id": "code_review", "name": "Code Review", "goal": "Review this code and suggest improvements: {description}", "default_amount": 1200, "category": "Engineering", "description": "Security, performance, and maintainability review"},
    {"id": "problem_solving", "name": "Problem Analysis", "goal": "Analyze and solve: {problem}", "default_amount": 1300, "category": "Consulting", "description": "Structured analysis with actionable recommendations"},
]


def _job_results_path() -> Path:
    """Path to persisted job results JSON (so View result works after restart)."""
    root = Path(__file__).resolve().parents[2]
    data_dir = Path(os.getenv("SOVEREIGN_DATA_DIR", str(root / "data")))
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / "job_results.json"


def _load_job_results() -> None:
    """Load _job_results from disk so View result shows past completed jobs."""
    global _job_results
    p = _job_results_path()
    if not p.exists():
        return
    try:
        with open(p, encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            _job_results = {int(k): v for k, v in raw.items() if str(k).isdigit() and isinstance(v, dict)}
    except Exception as e:
        logger.warning("Could not load job_results from %s: %s", p, e)


def _save_job_results() -> None:
    """Persist _job_results to disk (called after each job completion)."""
    p = _job_results_path()
    try:
        with open(p, "w", encoding="utf-8") as f:
            json.dump({str(k): v for k, v in _job_results.items()}, f, ensure_ascii=False)
    except Exception as e:
        logger.warning("Could not save job_results to %s: %s", p, e)


def _on_event(event_type: str, data: dict[str, Any]) -> None:
    """Called by GovernanceEngine; updates _tasks and _logs for the web UI."""
    global _tasks
    if event_type == "plan_created":
        _tasks = [
            {"task_id": t.get("task_id", ""), "skill": t.get("required_skill", ""), "status": "pending"}
            for t in data.get("tasks", [])
        ]
        _logs.append(("ceo", f"CEO: Plan created — {len(_tasks)} tasks. Goal: {(data.get('goal') or '')[:80]}..."))
    elif event_type == "cfo_approved":
        n = data.get("task_count", 0)
        est = data.get("estimated_cents", 0)
        bal = data.get("balance_cents", 0)
        _logs.append(("cfo", f"CFO: Approved {n} task(s), est. ${est/100:.2f}. Balance: ${bal/100:.2f}."))
    elif event_type == "task_started":
        task_id = data.get("task_id", "")
        agent_id = data.get("agent_id", "")
        for t in _tasks:
            if t.get("task_id") == task_id:
                t["status"] = "running"
                break
        skill = data.get("skill", "")
        skill_part = f" ({skill})" if skill else ""
        _logs.append(("cfo", f"CFO dispatch: Task {task_id}{skill_part} → {agent_id} (permission OK)."))
    elif event_type == "permission_denied":
        task_id = data.get("task_id", "")
        agent_id = data.get("agent_id", "")
        capability = data.get("capability", "")
        score = data.get("score", 0)
        threshold = data.get("threshold", 0)
        for t in _tasks:
            if t.get("task_id") == task_id:
                t["status"] = "failed"
                break
        _logs.append(("auditor_fail", f"Permission denied: Task {task_id} → {agent_id} (TrustScore {score} < {capability} {threshold})."))
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


def _enqueue_job(
    goal: str,
    charter: str,
    amount_cents: int = 0,
    currency: str = "USD",
    callback_url: str | None = None,
    delivery_contact: dict | None = None,
    dedup_within_seconds: int | None = None,
    priority: int = 0,
    run_after_ts: float | None = None,
) -> Job:
    """Create a new job in pending status. Requires human approval before execution. When dedup_within_seconds is set (e.g. by ingest poller), skip if a job with same goal+amount_cents was created within that window."""
    global _next_job_id, _jobs, _job_store
    amount_cents = max(0, int(amount_cents))
    currency = currency or "USD"
    callback_url = (callback_url or "").strip() or None
    if delivery_contact is not None and not isinstance(delivery_contact, dict):
        delivery_contact = None
    if dedup_within_seconds and dedup_within_seconds > 0:
        cutoff = time.time() - dedup_within_seconds
        for j in _jobs:
            if (
                (j.goal or "").strip() == (goal or "").strip()
                and getattr(j, "amount_cents", 0) == amount_cents
                and getattr(j, "created_ts", 0) >= cutoff
            ):
                _logs.append(("system", f"Ingest dedup: skipped duplicate of job {j.job_id} (goal+amount within {dedup_within_seconds}s)."))
                return j
    request_id = uuid.uuid4().hex
    if _job_store is not None:
        row = _job_store.add_job(
            goal, charter, amount_cents=amount_cents, currency=currency, callback_url=callback_url,
            delivery_contact=delivery_contact, priority=priority, run_after_ts=run_after_ts,
        )
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
            callback_url=row.callback_url,
            retry_count=getattr(row, "retry_count", 0),
            request_id=request_id,
            priority=getattr(row, "priority", 0),
            run_after_ts=getattr(row, "run_after_ts", None),
            delivery_contact=getattr(row, "delivery_contact", None),
        )
        _next_job_id = row.job_id + 1
    else:
        job = Job(
            job_id=_next_job_id,
            goal=goal,
            charter=charter,
            amount_cents=amount_cents,
            currency=currency,
            callback_url=callback_url,
            retry_count=0,
            request_id=request_id,
            priority=priority,
            run_after_ts=run_after_ts,
            delivery_contact=delivery_contact,
        )
        _next_job_id += 1
    _jobs.append(job)
    auto_approve = _effective_auto_approve()
    if auto_approve:
        job.status = "approved"
        job.updated_ts = time.time()
        if _job_store is not None:
            _job_store.update_job(job.job_id, status="approved")
            push_approved = getattr(_job_store, "push_approved", None)
            if callable(push_approved):
                push_approved(job.job_id)
        _logs.append(("system", f"Job {job.job_id} created and auto-approved (human-out-of-loop)."))
    else:
        _logs.append(("system", f"Job {job.job_id} created (pending approval)."))
    return job


def _fire_job_webhook(
    job: Job,
    status: str,
    results: list[Any],
    reports: list[Any],
) -> None:
    """If SOVEREIGN_WEBHOOK_URL or job.callback_url is set, POST completion payload (with retries)."""
    url = (job.callback_url or "").strip() or (os.getenv("SOVEREIGN_WEBHOOK_URL") or "").strip()
    if not url:
        return
    result_summary = "\n".join(getattr(r, "output", "") for r in (results or [])).strip() or ""
    audit_score = (
        sum(getattr(r, "score", 0.0) for r in (reports or [])) / len(reports)
        if reports else 0.0
    )
    secret = (os.getenv("SOVEREIGN_WEBHOOK_SECRET") or "").strip() or None
    request_id = getattr(job, "request_id", None)
    try:
        from sovereign_os.web.job_webhook import notify_job_completion
        notify_job_completion(
            webhook_url=url,
            job_id=job.job_id,
            status=status,
            goal=job.goal,
            amount_cents=job.amount_cents,
            currency=job.currency or "USD",
            payment_id=job.payment_id,
            completed_at=datetime.now(timezone.utc).isoformat(),
            result_summary=result_summary,
            audit_score=audit_score,
            charter=job.charter or "Default",
            secret=secret,
            request_id=request_id,
        )
    except Exception as e:
        logger.warning("Job completion webhook failed for job_id=%s request_id=%s: %s", job.job_id, request_id, e)
    else:
        _logs.append(("system", "Delivery: result sent to webhook."))
    # If job has delivery_contact (e.g. from Reddit ingest), post result back to the client
    dc = getattr(job, "delivery_contact", None)
    if isinstance(dc, dict) and dc.get("platform") == "reddit" and status in ("completed", "payment_failed"):
        result_summary = "\n".join(getattr(r, "output", "") for r in (results or [])).strip() or ""
        try:
            from sovereign_os.delivery.reddit import deliver_result_to_reddit
            if deliver_result_to_reddit(dc, result_summary, job.job_id):
                _logs.append(("system", "Delivery: result posted to Reddit."))
        except Exception as e:
            logger.warning("Reddit delivery failed for job_id=%s: %s", job.job_id, e)


def _run_one_job(job: Job) -> None:
    """
    Execute a single approved job: run mission with audit, then on full success
    charge via PaymentService and record income in UnifiedLedger.
    """
    global _engine, _ledger, _payment_service
    start_time = time.time()
    if _engine is None:
        job.status = "failed"
        job.error = "Engine not configured"
        job.updated_ts = time.time()
        _logs.append(("auditor_fail", f"Job {job.job_id} failed: {job.error}"))
        try:
            from sovereign_os.telemetry.tracer import record_job_completed
            record_job_completed("failed", time.time() - start_time)
        except Exception:
            pass
        return

    job.status = "running"
    job.updated_ts = time.time()
    if _job_store is not None:
        _job_store.update_job(job.job_id, status="running")
    req_id = getattr(job, "request_id", None) or ""
    _logs.append(("ceo", f"Job {job.job_id} running: {job.goal[:80]}{'…' if len(job.goal) > 80 else ''}" + (f" [request_id={req_id}]" if req_id else "")))
    _job_progress[job.job_id] = {"stage": "planning", "tasks_total": 0, "tasks_done": 0, "current_task": "", "pct": 5}

    async def _run_mission_and_settle() -> None:
        _job_progress[job.job_id] = {"stage": "planning", "tasks_total": 0, "tasks_done": 0, "current_task": "CEO decomposing goal...", "pct": 10}
        plan, results, reports = await _engine.run_mission_with_audit(
            job.goal, abort_on_audit_failure=False, job_revenue_cents=job.amount_cents
        )
        task_by_id = {t.task_id: t for t in plan.tasks} if plan else {}
        n_tasks = len(results) if results else 0
        _job_progress[job.job_id] = {
            "stage": "auditing", "tasks_total": n_tasks, "tasks_done": n_tasks,
            "current_task": "Verifying deliverables...", "pct": 85,
        }
        audit_details = []
        if reports:
            for rep in reports:
                audit_details.append({
                    "task_id": getattr(rep, "task_id", ""),
                    "passed": getattr(rep, "passed", False),
                    "score": getattr(rep, "score", 0.0),
                    "reason": getattr(rep, "reason", ""),
                    "suggested_fix": getattr(rep, "suggested_fix", ""),
                })
        _job_results[job.job_id] = {
            "goal": job.goal[:1000],
            "tasks": [
                {
                    "task_id": r.task_id,
                    "skill": getattr(task_by_id.get(r.task_id), "required_skill", ""),
                    "output": (r.output or "")[:100000],
                    "success": r.success,
                }
                for r in results
            ],
            "combined_output": "\n\n---\n\n".join(r.output or "" for r in results)[:150000],
            "audit_reports": audit_details,
        }
        try:
            _save_job_results()
        except Exception as e:
            logger.warning("Save job results failed for job %s: %s", job.job_id, e)
        all_passed = all(getattr(r, "passed", False) for r in reports) if reports else True
        if all_passed:
            _job_progress[job.job_id] = {"stage": "settling", "tasks_total": n_tasks, "tasks_done": n_tasks, "current_task": "Processing payment...", "pct": 95}
        if not all_passed:
            job.status = "failed"
            job.error = "One or more tasks failed audit"
            job.updated_ts = time.time()
            if _job_store is not None:
                _job_store.update_job(job.job_id, status="failed", error=job.error)
            _logs.append(("auditor_fail", f"Job {job.job_id} failed: audit did not pass."))
            if job.job_id in _job_results:
                _job_results[job.job_id]["status"] = "failed"
                _job_results[job.job_id]["error"] = job.error
                # Keep audit_reports so View result can show Judge reason/suggested_fix
            try:
                _save_job_results()
            except Exception as e:
                logger.warning("Save job results (audit failed) for job %s: %s", job.job_id, e)
            return
        if job.amount_cents <= 0:
            job.status = "completed"
            job.updated_ts = time.time()
            if _job_store is not None:
                _job_store.update_job(job.job_id, status="completed")
            _logs.append(("auditor_pass", f"Job {job.job_id} completed (no charge)."))
            _fire_job_webhook(job, "completed", results, reports)
            return
        if _payment_service is None:
            job.status = "completed"
            job.updated_ts = time.time()
            if _job_store is not None:
                _job_store.update_job(job.job_id, status="completed")
            _logs.append(("system", f"Job {job.job_id} completed (no payment service; income not recorded)."))
            _fire_job_webhook(job, "completed", results, reports)
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
            _job_progress[job.job_id] = {"stage": "done", "tasks_total": n_tasks, "tasks_done": n_tasks, "current_task": "", "pct": 100}
            _fire_job_webhook(job, "completed", results, reports)
            try:
                from sovereign_os.notifications import notify_job_event
                avg_score = sum(getattr(r, "score", 0) for r in reports) / len(reports) if reports else None
                notify_job_event("job_completed", job.job_id, job.goal, "completed", job.amount_cents, job.currency or "USD", "\n".join(r.output or "" for r in results)[:1000], avg_score)
            except Exception:
                pass
        except Exception as e:
            job.status = "payment_failed"
            job.error = str(e)
            job.updated_ts = time.time()
            if _job_store is not None:
                _job_store.update_job(job.job_id, status="payment_failed", error=job.error)
            _logs.append(("auditor_fail", f"Job {job.job_id} payment failed: {e}"))
            logger.exception("Job %s payment failed", job.job_id)
            _fire_job_webhook(job, "payment_failed", results, reports)

    async def _run() -> None:
        try:
            await _run_mission_and_settle()
        except Exception as e:
            from sovereign_os.governance.exceptions import HumanApprovalRequiredError, UnprofitableJobError
            from sovereign_os.agents.auth import PermissionDeniedError

            if isinstance(e, HumanApprovalRequiredError):
                job.status = "pending"
                job.error = str(e)
                job.updated_ts = time.time()
                if _job_store is not None:
                    _job_store.update_job(job.job_id, status="pending", error=job.error)
                _logs.append(("cfo", f"Job {job.job_id}: human approval required for spend — {e}"))
                logger.warning("Job %s: %s", job.job_id, e)
                return
            if isinstance(e, UnprofitableJobError):
                job.status = "failed"
                job.error = str(e)
                job.updated_ts = time.time()
                if _job_store is not None:
                    _job_store.update_job(job.job_id, status="failed", error=job.error)
                _job_results[job.job_id] = {
                    "goal": job.goal[:1000],
                    "tasks": [],
                    "combined_output": "",
                    "status": "failed",
                    "error": job.error,
                }
                try:
                    _save_job_results()
                except Exception as save_err:
                    logger.warning("Save job results (unprofitable) for job %s: %s", job.job_id, save_err)
                _logs.append(("cfo", f"Job {job.job_id} rejected: unprofitable (est. cost > revenue margin floor). {e}"))
                logger.warning("Job %s: CFO rejected unprofitable job. %s", job.job_id, e)
                return
            job.status = "failed"
            job.error = "permission denied for a task" if isinstance(e, PermissionDeniedError) else str(e)
            job.updated_ts = time.time()
            if _job_store is not None:
                _job_store.update_job(job.job_id, status="failed", error=job.error)
            _job_results[job.job_id] = {
                "goal": job.goal[:1000],
                "tasks": [],
                "combined_output": "",
                "status": "failed",
                "error": job.error,
            }
            try:
                _save_job_results()
            except Exception as save_err:
                logger.warning("Save job results (mission failed) for job %s: %s", job.job_id, save_err)
            if isinstance(e, PermissionDeniedError):
                _logs.append(("auditor_fail", f"Job {job.job_id} failed: permission denied (see Decision stream)."))
            else:
                _logs.append(("auditor_fail", f"Mission error for Job {job.job_id}: {e}"))
            logger.exception("Job %s mission failed", job.job_id)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_run())
    finally:
        loop.close()
    if job.status in ("completed", "failed", "payment_failed"):
        global _last_job_completed_at
        _last_job_completed_at = time.time()
        try:
            from sovereign_os.telemetry.tracer import record_job_completed
            record_job_completed(job.status, time.time() - start_time)
        except Exception:
            pass


def _run_one_job_with_sem(job: Job) -> None:
    """Run one job and release concurrency semaphore when done."""
    try:
        _run_one_job(job)
    finally:
        if _job_concurrency_semaphore is not None:
            _job_concurrency_semaphore.release()


def _job_row_to_job(row: Any) -> Job:
    """Build a Job from a JobRow (store)."""
    return Job(
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
        callback_url=row.callback_url,
        retry_count=getattr(row, "retry_count", 0),
        request_id=None,
        priority=getattr(row, "priority", 0),
        run_after_ts=getattr(row, "run_after_ts", None),
        delivery_contact=getattr(row, "delivery_contact", None),
    )


def _job_worker() -> None:
    """Background thread: pick approved jobs and run them (up to SOVEREIGN_JOB_WORKER_CONCURRENCY in parallel). Respects _shutdown_requested. Orders by priority (higher first) and run_after_ts (ready first). When store has pop_approved (Redis), claim from shared queue."""
    global _shutdown_requested
    concurrency = max(1, int(os.getenv("SOVEREIGN_JOB_WORKER_CONCURRENCY", "1")))
    pop_approved = getattr(_job_store, "pop_approved", None) if _job_store else None
    while not _shutdown_requested:
        if pop_approved and callable(pop_approved):
            job_id = pop_approved(timeout=5.0)
            if job_id is not None and _job_store is not None:
                row = _job_store.get_job(job_id)
                if row and row.status == "approved":
                    job = _job_row_to_job(row)
                    _jobs.append(job)  # so UI lists it
                    if _job_concurrency_semaphore is not None:
                        if not _job_concurrency_semaphore.acquire(blocking=False):
                            # Re-queue: push back (best-effort)
                            if getattr(_job_store, "push_approved", None):
                                _job_store.push_approved(job_id)
                            continue
                        Thread(target=_run_one_job_with_sem, args=(job,), daemon=False).start()
                    else:
                        _run_one_job(job)
                    continue
            time.sleep(1)
            continue
        time.sleep(5)
        if _shutdown_requested:
            break
        now = time.time()
        approved = [
            j for j in _jobs
            if j.status == "approved"
            and (getattr(j, "run_after_ts", None) is None or getattr(j, "run_after_ts", 0) <= now)
        ]
        approved.sort(key=lambda j: (-getattr(j, "priority", 0), getattr(j, "run_after_ts") or 0))
        if _job_concurrency_semaphore is None:
            for j in approved:
                _run_one_job(j)
                break
        else:
            if not _job_concurrency_semaphore.acquire(blocking=False):
                continue
            for j in approved:
                Thread(target=_run_one_job_with_sem, args=(j,), daemon=False).start()
                break
            else:
                _job_concurrency_semaphore.release()
    logger.info("Job worker exiting (shutdown_requested=%s)", _shutdown_requested)


_DASHBOARD_TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "dashboard.html"


def _get_dashboard_html() -> str:
    """Return dashboard HTML (template at load time or embedded fallback)."""
    return _EMBEDDED_DASHBOARD


def _load_dashboard_html() -> str:
    """Load from template file if present, else use embedded default (same layout as template)."""
    if _DASHBOARD_TEMPLATE_PATH.exists():
        return _DASHBOARD_TEMPLATE_PATH.read_text(encoding="utf-8")
    return _DEFAULT_EMBEDDED_DASHBOARD


# Embedded dashboard: synced with templates/dashboard.html (dual-column, cards, health, token usage).
# When template exists, _load_dashboard_html() uses it at module init so _EMBEDDED_DASHBOARD stays in sync.
_EMBEDDED_DASHBOARD = ""
_DEFAULT_EMBEDDED_DASHBOARD = """<!DOCTYPE html>
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
      --bg: #f5f4f1;
      --bg-card: #ffffff;
      --text: #0f0f0f;
      --text-soft: #525252;
      --text-muted: #737373;
      --border: rgba(0,0,0,0.06);
      --black: #0f0f0f;
      --success: #15803d;
      --warn: #a16207;
      --danger: #b91c1c;
      --radius: 14px;
      --shadow: 0 1px 3px rgba(0,0,0,0.06);
      --shadow-hover: 0 8px 24px rgba(0,0,0,0.08);
      --ease-out: cubic-bezier(0.22, 1, 0.36, 1);
      --ease-in-out: cubic-bezier(0.65, 0, 0.35, 1);
      --dur: 0.35s;
      --dur-fast: 0.2s;
    }
    @keyframes fadeInUp {
      from { opacity: 0; transform: translateY(12px); }
      to { opacity: 1; transform: translateY(0); }
    }
    @keyframes fadeIn {
      from { opacity: 0; }
      to { opacity: 1; }
    }
    @keyframes pulse-soft {
      0%, 100% { opacity: 1; transform: scale(1); }
      50% { opacity: 0.85; transform: scale(1.08); }
    }
    @keyframes slideDown {
      from { opacity: 0; transform: translateY(-8px); }
      to { opacity: 1; transform: translateY(0); }
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    html { scroll-behavior: smooth; }
    body {
      font-family: 'Poppins', -apple-system, BlinkMacSystemFont, sans-serif;
      background: var(--bg);
      color: var(--text);
      min-height: 100vh;
      font-size: 15px;
      line-height: 1.55;
      -webkit-font-smoothing: antialiased;
      text-align: left;
      animation: fadeIn var(--dur) var(--ease-out);
    }
    .topbar {
      background: var(--bg-card);
      border-bottom: 1px solid var(--border);
      padding: 16px 28px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      box-shadow: var(--shadow);
      animation: slideDown 0.4s var(--ease-out);
      position: relative;
      z-index: 10;
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
      animation: pulse-soft 2.5s var(--ease-in-out) infinite;
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
      max-width: 1180px;
      margin: 0 auto;
      padding: 32px 24px 48px;
      text-align: left;
    }
    .hero {
      margin-bottom: 28px;
      text-align: left;
      animation: fadeInUp 0.5s var(--ease-out) 0.05s both;
    }
    .hero h1 {
      font-size: 1.75rem;
      font-weight: 700;
      letter-spacing: -0.03em;
      color: var(--black);
      line-height: 1.25;
      margin-bottom: 6px;
    }
    .hero p {
      font-size: 0.9375rem;
      color: var(--text-soft);
      font-weight: 400;
    }
    .grid-2 {
      display: grid;
      grid-template-columns: 1fr 360px;
      gap: 24px;
      align-items: start;
    }
    @media (max-width: 900px) { .grid-2 { grid-template-columns: 1fr; } }
    .card {
      background: var(--bg-card);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 22px 24px;
      box-shadow: var(--shadow);
      transition: box-shadow var(--dur) var(--ease-out), transform var(--dur-fast) var(--ease-out), border-color var(--dur-fast);
    }
    .card:hover {
      box-shadow: var(--shadow-hover);
      transform: translateY(-2px);
      border-color: rgba(0,0,0,0.08);
    }
    .main .card:nth-child(1) { animation: fadeInUp 0.45s var(--ease-out) 0.1s both; }
    .main .card:nth-child(2) { animation: fadeInUp 0.45s var(--ease-out) 0.18s both; }
    .main .card:nth-child(3) { animation: fadeInUp 0.45s var(--ease-out) 0.26s both; }
    .main .card:nth-child(4) { animation: fadeInUp 0.45s var(--ease-out) 0.34s both; }
    .sidebar .card:nth-child(1) { animation: fadeInUp 0.45s var(--ease-out) 0.14s both; }
    .sidebar .card:nth-child(2) { animation: fadeInUp 0.45s var(--ease-out) 0.22s both; }
    .sidebar .card:nth-child(3) { animation: fadeInUp 0.45s var(--ease-out) 0.3s both; }
    .sidebar .card:nth-child(4) { animation: fadeInUp 0.45s var(--ease-out) 0.38s both; }
    .card-title {
      font-size: 0.75rem;
      font-weight: 600;
      letter-spacing: 0.04em;
      text-transform: uppercase;
      color: var(--text-muted);
      margin-bottom: 6px;
    }
    .card-heading {
      font-size: 1.0625rem;
      font-weight: 700;
      color: var(--black);
      margin-bottom: 4px;
    }
    .card-desc {
      font-size: 0.8125rem;
      color: var(--text-muted);
      margin-bottom: 14px;
    }
    .prompt-row {
      display: flex;
      gap: 10px;
      align-items: stretch;
    }
    .prompt-row input {
      flex: 1;
      padding: 14px 18px;
      font-size: 0.9375rem;
      font-family: inherit;
      background: var(--bg);
      border: 1px solid var(--border);
      border-radius: 10px;
      color: var(--black);
      transition: border-color var(--dur) var(--ease-out), box-shadow var(--dur) var(--ease-out), background var(--dur-fast);
    }
    .prompt-row input::placeholder { color: var(--text-muted); }
    .prompt-row input:focus {
      outline: none;
      border-color: var(--black);
      box-shadow: 0 0 0 2px rgba(0,0,0,0.06);
    }
    .btn {
      padding: 14px 24px;
      font-size: 0.9375rem;
      font-weight: 600;
      font-family: inherit;
      background: var(--black);
      color: #fff;
      border: none;
      border-radius: 10px;
      cursor: pointer;
      transition: background 0.15s, box-shadow 0.15s, transform 0.15s;
    }
    .btn:hover {
      background: #262626;
      box-shadow: 0 4px 12px rgba(0,0,0,0.15);
      transform: translateY(-1px);
    }
    .btn:active {
      transform: translateY(0);
      box-shadow: 0 2px 6px rgba(0,0,0,0.12);
    }
    .section-title {
      font-size: 1.0625rem;
      font-weight: 700;
      color: var(--black);
      margin-bottom: 2px;
      letter-spacing: -0.01em;
    }
    .section-subtitle {
      font-size: 0.8125rem;
      color: var(--text-muted);
      margin-bottom: 14px;
    }
    .feed {
      background: var(--bg);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 4px 0;
      max-height: 320px;
      overflow-y: auto;
    }
    .feed-item {
      padding: 12px 18px;
      font-size: 0.875rem;
      line-height: 1.5;
      border-bottom: 1px solid var(--border);
      color: var(--text-soft);
      transition: background var(--dur-fast), color var(--dur-fast);
    }
    .feed-item:hover { background: rgba(0,0,0,0.02); }
    .feed-item:last-child { border-bottom: none; }
    .feed-item.ceo { color: var(--black); font-weight: 500; }
    .feed-item.cfo { color: var(--black); opacity: 0.9; }
    .feed-item.auditor_pass { color: var(--success); }
    .feed-item.auditor_fail { color: var(--danger); }
    .tasks-row {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-top: 12px;
    }
    .task-pill {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 10px 14px;
      font-size: 0.875rem;
      background: var(--bg);
      border: 1px solid var(--border);
      border-radius: 10px;
      color: var(--text-soft);
      transition: background var(--dur-fast) var(--ease-out), border-color var(--dur-fast), transform var(--dur-fast) var(--ease-out), box-shadow var(--dur-fast);
    }
    .task-pill:hover { transform: translateY(-1px); box-shadow: 0 2px 8px rgba(0,0,0,0.06); }
    .task-pill .dot {
      width: 8px; height: 8px; border-radius: 50%;
    }
    .task-pill.pending .dot { background: var(--text-muted); }
    .task-pill.running .dot {
      background: var(--black);
      animation: pulse-soft 1.2s var(--ease-in-out) infinite;
    }
    .task-pill.passed .dot { background: var(--success); }
    .task-pill.failed .dot { background: var(--danger); }
    .task-pill .id { font-weight: 600; color: var(--black); }
    .task-pill.job-completed { border-color: rgba(22, 101, 52, 0.4); color: var(--success); }
    .task-pill.job-failed, .task-pill.job-payment_failed { border-color: rgba(153, 27, 27, 0.4); color: var(--danger); }
    .task-pill.job-running { border-color: rgba(10, 10, 10, 0.3); }
    .stats {
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      gap: 20px;
      margin-top: 28px;
      padding-top: 24px;
      border-top: 1px solid var(--border);
    }
    @media (max-width: 640px) { .stats { grid-template-columns: repeat(2, 1fr); } }
    .stat {
      animation: fadeInUp 0.4s var(--ease-out) both;
    }
    .stat:nth-child(1) { animation-delay: 0.42s; }
    .stat:nth-child(2) { animation-delay: 0.48s; }
    .stat:nth-child(3) { animation-delay: 0.54s; }
    .stat:nth-child(4) { animation-delay: 0.6s; }
    .stat .label { font-size: 0.75rem; color: var(--text-muted); margin-bottom: 4px; text-transform: uppercase; letter-spacing: 0.03em; }
    .stat .value {
      font-size: 1.25rem;
      font-weight: 700;
      color: var(--black);
      font-variant-numeric: tabular-nums;
      letter-spacing: -0.02em;
      transition: transform var(--dur-fast) var(--ease-out);
    }
    .stat:hover .value { transform: scale(1.02); }
    .empty-feed { padding: 24px 20px; color: var(--text-muted); font-size: 0.875rem; }
    .health-box {
      display: inline-flex; align-items: center; gap: 14px; flex-wrap: wrap;
      padding: 12px 16px; background: var(--bg); border: 1px solid var(--border);
      border-radius: 10px; font-size: 0.875rem;
    }
    .health-box .status { font-weight: 600; }
    .health-box .status.ok { color: var(--success); }
    .health-box .status.degraded, .health-box .status.error { color: var(--danger); }
    .health-box span + span { margin-left: 8px; }
    .token-table { width: 100%; border-collapse: collapse; font-size: 0.8125rem; background: var(--bg); border-radius: 10px; overflow: hidden; border: 1px solid var(--border); }
    .token-table th, .token-table td { padding: 10px 14px; text-align: left; border-bottom: 1px solid var(--border); }
    .token-table th { background: rgba(0,0,0,0.04); font-weight: 600; color: var(--black); font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.03em; }
    .token-table tr:last-child td { border-bottom: none; }
    .footer-strip {
      margin-top: 32px;
      padding-top: 16px;
      border-top: 1px solid var(--border);
      font-size: 0.8125rem;
      color: var(--text-muted);
      animation: fadeIn 0.5s var(--ease-out) 0.65s both;
    }
    .sidebar .card { margin-bottom: 20px; }
    .sidebar .card:last-child { margin-bottom: 0; }
    .audit-trail-feed { max-height: 180px; overflow-y: auto; font-size: 0.8125rem; }
    .audit-trail-item { padding: 8px 0; border-bottom: 1px solid var(--border); color: var(--text-soft); }
    .audit-trail-item:last-child { border-bottom: none; }
    .audit-trail-item .task-id { font-weight: 600; color: var(--black); }
    .audit-trail-item .verified { color: var(--success); }
    .audit-trail-item .unverified { color: var(--danger); }
  </style>
</head>
<body>
  <header class="topbar">
    <span class="topbar-brand"><span class="logo-mark"></span>Sovereign-OS</span>
    <span class="topbar-meta">Charter <strong id="charter">—</strong> · Balance <strong id="balance">—</strong> · Tokens <strong id="tokens">—</strong> · Trust <strong id="trust">—</strong> · <a href="/health" target="_blank" style="color:var(--text-muted);text-decoration:none">Health</a></span>
  </header>
  <div class="wrap">
    <header class="hero">
      <h1>Command Center</h1>
      <p>Run missions, approve jobs, and monitor the audit stream. Ledger · CEO/CFO · 24/7 jobs · <a href="/health" target="_blank" style="color:var(--text-soft);text-decoration:none">/health</a></p>
    </header>
    <div class="grid-2">
      <div class="main">
        <section class="card" style="margin-bottom: 24px;">
          <div class="card-title">Mission</div>
          <div class="card-heading">Run a mission</div>
          <p class="card-desc">Describe the goal; the entity will plan, get CFO approval, dispatch agents, and audit.</p>
          <div class="prompt-row">
            <input type="text" id="goal" placeholder="e.g. Summarize the market in one paragraph" value="Summarize the market in one paragraph." />
            <button class="btn" onclick="runMission()">Run</button>
          </div>
        </section>
        <section class="card" style="margin-bottom: 24px;">
          <div class="card-title">Task DAG</div>
          <div class="card-heading">Tasks</div>
          <p class="card-desc">Current or last mission task status (pending → running → passed/failed).</p>
          <div class="tasks-row" id="tasks"></div>
        </section>
        <section class="card" style="margin-bottom: 24px;">
          <div class="card-title">Audit trail</div>
          <div class="card-heading">Activity</div>
          <p class="card-desc">Plans, approvals, execution, and audit results in real time.</p>
          <div class="feed" id="logs"></div>
        </section>
      </div>
      <aside class="sidebar">
        <section class="card">
          <div class="card-title">System</div>
          <div class="card-heading">Health check</div>
          <p class="card-desc">Ledger and Redis. Refreshed every 10s.</p>
          <div class="health-box" id="health">
            <span class="status">—</span><span>Ledger: —</span><span>Redis: —</span>
          </div>
        </section>
        <section class="card">
          <div class="card-title">Cost</div>
          <div class="card-heading">Token usage</div>
          <p class="card-desc">Per task and per agent from the ledger.</p>
          <div id="tokenUsage"><span class="empty-feed">No token records yet.</span></div>
        </section>
        <section class="card">
          <div class="card-title">Operations</div>
          <div class="card-heading">Job queue</div>
          <p class="card-desc">External jobs; approve to run 24/7.</p>
          <div class="tasks-row" id="jobs"></div>
        </section>
        <section class="card">
          <div class="card-title">Phase 6a</div>
          <div class="card-heading">Audit trail</div>
          <p class="card-desc">Recent audits (proof_hash). Set SOVEREIGN_AUDIT_TRAIL_PATH to enable.</p>
          <div id="auditTrail" class="audit-trail-feed"><span class="empty-feed">No audit trail. Run missions and set SOVEREIGN_AUDIT_TRAIL_PATH.</span></div>
        </section>
      </aside>
    </div>
    <div class="stats">
      <div class="stat"><div class="label">Balance</div><div class="value" id="finBalance">$0.00</div></div>
      <div class="stat"><div class="label">Tokens burned</div><div class="value" id="finTokens">0</div></div>
      <div class="stat"><div class="label">Agent</div><div class="value" id="finAgent">—</div></div>
      <div class="stat"><div class="label">Trust score</div><div class="value" id="finTrust">—</div></div>
    </div>
    <div class="footer-strip">
      Sovereign‑OS is experimental. Keep a human in the loop for financial and production decisions.
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
          let meta = '';
          if (j.priority && j.priority > 0) meta += ' P' + j.priority;
          if (j.run_after_ts) { const d = new Date(j.run_after_ts * 1000); meta += ' after ' + d.toISOString().slice(0,16).replace('T',' '); }
          const approveBtn = j.status === 'pending'
            ? ' <button class="btn" style="padding:6px 10px;font-size:0.8rem" onclick="approveJob(' + j.job_id + ')">Approve</button>'
            : '';
          return '<div class=\"' + cls + '\"><span class=\"id\">' + label + '</span><span>' + amount + status + (meta ? '<span style="color:var(--text-muted);font-size:0.85em">' + meta + '</span>' : '') + '</span>' + approveBtn + '</div>';
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
    function fetchHealth() {
      fetch('/health').then(x => x.json()).then(d => {
        const el = r('health');
        if (!el) return;
        const status = (d.status || 'error').toLowerCase();
        const ledger = d.ledger === true ? '✓' : (d.ledger === false ? '✗' : '—');
        const redis = d.redis === true ? '✓' : (d.redis === false ? '✗' : '—');
        let mode = '';
        if (d.auto_approve_jobs === true) mode += ' Auto-approve ON';
        if (d.compliance_auto_proceed === true) mode += ' Compliance auto ON';
        if (mode) mode = '<span style="color:var(--success)">' + mode.trim() + '</span>';
        el.innerHTML = '<span class="status ' + status + '">' + escapeHtml(status.toUpperCase()) + '</span><span>Ledger: ' + ledger + '</span><span>Redis: ' + redis + '</span>' + (mode ? mode : '');
      }).catch(() => {
        const el = r('health');
        if (el) el.innerHTML = '<span class="status error">ERROR</span><span>Failed to fetch /health</span>';
      });
    }
    function fetchTokenUsage() {
      fetch('/api/token_usage').then(x => x.json()).then(d => {
        const el = r('tokenUsage');
        if (!el) return;
        const rows = d.token_usage || [];
        if (!rows.length) {
          el.innerHTML = '<span class="empty-feed">No token records yet. Run a mission to see usage.</span>';
          return;
        }
        el.innerHTML = '<table class="token-table"><thead><tr><th>Task</th><th>Agent</th><th>Model</th><th>Input</th><th>Output</th><th>Total</th></tr></thead><tbody>' +
          rows.map(u => '<tr><td>' + escapeHtml(u.task_id) + '</td><td>' + escapeHtml(u.agent_id) + '</td><td>' + escapeHtml(u.model_id) + '</td><td>' + (u.input_tokens || 0) + '</td><td>' + (u.output_tokens || 0) + '</td><td>' + (u.total_tokens || 0) + '</td></tr>').join('') +
          '</tbody></table>';
      }).catch(() => {});
    }
    function fetchAuditTrail() {
      fetch('/api/audit_trail?limit=10').then(x => x.json()).then(d => {
        const el = r('auditTrail');
        if (!el) return;
        const list = d.audit_trail || [];
        if (!list.length) {
          el.innerHTML = '<span class="empty-feed">' + escapeHtml(d.message || 'No audit trail. Set SOVEREIGN_AUDIT_TRAIL_PATH.') + '</span>';
          return;
        }
        el.innerHTML = list.slice(0, 10).map(e => {
          const cls = e.verified ? 'verified' : 'unverified';
          const hash = (e.proof_hash || '').slice(0, 8);
          return '<div class="audit-trail-item"><span class="task-id">' + escapeHtml(e.task_id) + '</span> ' + (e.passed ? 'PASS' : 'FAIL') + ' · ' + (e.score != null ? e.score : '') + ' <span class="' + cls + '">' + (e.verified ? '✓' : '✗') + '</span> ' + hash + '</div>';
        }).join('');
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
    setInterval(fetchHealth, 10000);
    setInterval(fetchTokenUsage, 3000);
    setInterval(fetchAuditTrail, 8000);
    fetchStatus(); fetchTasks(); fetchLogs(); fetchJobs(); fetchHealth(); fetchTokenUsage(); fetchAuditTrail();
  </script>
</body>
</html>
"""

_EMBEDDED_DASHBOARD = _load_dashboard_html()

# Rate limit for POST /api/jobs: client_id -> list of request timestamps (pruned to last 60s)
_job_rate_limit_times: dict[str, list[float]] = {}

# Job validation limits (aligned with OPTIMIZATION_ROADMAP)
JOB_GOAL_MAX_LEN = 20_000
JOB_AMOUNT_CENTS_MIN = 0
JOB_AMOUNT_CENTS_MAX = 1_000_000


def _callback_url_ssrf_safe(callback_url: str) -> None:
    """Raise ValueError if callback_url host is private/local (SSRF protection)."""
    from urllib.parse import urlparse
    parsed = urlparse(callback_url)
    host = (parsed.hostname or "").strip().lower()
    if not host:
        return
    if host in ("localhost", "localhost.", "::1"):
        raise ValueError("callback_url must not point to localhost (SSRF protection)")
    try:
        import ipaddress
        addr = ipaddress.ip_address(host)
        if addr.is_private or addr.is_loopback or addr.is_link_local:
            raise ValueError("callback_url must not point to private or loopback IP (SSRF protection)")
    except ValueError as e:
        if "must not" in str(e):
            raise
        pass  # host is a hostname (e.g. example.com), not an IP — allow


def validate_job_input(
    goal: str,
    amount_cents: int,
    callback_url: str | None,
    *,
    goal_max_len: int = JOB_GOAL_MAX_LEN,
    amount_min: int = JOB_AMOUNT_CENTS_MIN,
    amount_max: int = JOB_AMOUNT_CENTS_MAX,
    ssrf_check: bool = True,
) -> None:
    """Validate job fields. Raises ValueError with a message if invalid. Used by API and tests."""
    from urllib.parse import urlparse
    if len(goal) > goal_max_len:
        raise ValueError(f"goal length exceeds {goal_max_len}")
    if amount_cents < amount_min or amount_cents > amount_max:
        raise ValueError(f"amount_cents must be between {amount_min} and {amount_max}")
    if callback_url:
        try:
            parsed = urlparse(callback_url)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                raise ValueError("callback_url must be a valid http(s) URL")
            if ssrf_check:
                _callback_url_ssrf_safe(callback_url)
        except ValueError:
            raise
        except Exception as e:
            raise ValueError("callback_url must be a valid http(s) URL") from e


def _api_key_dependency():
    """Dependency: require X-API-Key when API key is required (overlay or env)."""
    from fastapi import Header, HTTPException
    o = _get_ui_overrides()
    acc = o.get("access") or {}
    if acc.get("api_key_required") is False:
        def _noop():
            return
        return _noop
    key = os.getenv("SOVEREIGN_API_KEY")
    if not key:
        def _noop():
            return
        return _noop
    def _verify(x_api_key: str | None = Header(None), authorization: str | None = Header(None)):
        token = x_api_key or (authorization.split(" ", 1)[-1] if authorization and str(authorization).lower().startswith("bearer ") else None)
        if not token or not (key and _secure_compare(token, key)):
            raise HTTPException(status_code=401, detail="Invalid or missing API key")
    return _verify


def _secure_compare(a: str, b: str) -> bool:
    """Constant-time comparison to avoid timing attacks on API key."""
    import hmac
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def create_app(
    engine: Any = None,
    ledger: Any = None,
    auth: Any = None,
    charter_name: str = "Default",
    charter_path: str | None = None,
) -> Any:
    """Create FastAPI app with dashboard and API. Optionally inject engine/ledger/auth."""
    try:
        from fastapi import Body, Depends, FastAPI, Request
        from fastapi.responses import HTMLResponse, JSONResponse
    except ImportError:
        raise ImportError("fastapi required for web UI; pip install fastapi uvicorn")

    global _engine, _ledger, _auth, _charter_name, _charter_path
    _engine = engine
    _ledger = ledger
    _auth = auth
    _charter_name = charter_name
    _charter_path = charter_path
    if engine is not None:
        engine._on_event = _on_event

    app = FastAPI(title="Sovereign-OS Command Center (Web)")

    def _config_warnings() -> list[str]:
        """Warnings when minimal config for paid/demo use is missing or production-unsafe."""
        w: list[str] = []
        if not (os.getenv("STRIPE_API_KEY") or "").strip():
            w.append("STRIPE_API_KEY not set (payments will use DummyPaymentService)")
        stripe_key = (os.getenv("STRIPE_API_KEY") or "").strip()
        if stripe_key and "sk_live_" in stripe_key:
            w.append("Live Stripe key (sk_live_) detected; ensure this is intended for production")
        if stripe_key and not (os.getenv("SOVEREIGN_API_KEY") or "").strip():
            w.append("SOVEREIGN_API_KEY not set (recommended for production to protect POST /api/jobs)")
        openai = (os.getenv("OPENAI_API_KEY") or "").strip()
        anthropic = (os.getenv("ANTHROPIC_API_KEY") or "").strip()
        if not openai and not anthropic:
            w.append("No LLM key set (set OPENAI_API_KEY or ANTHROPIC_API_KEY for real workers)")
        return w

    def _job_rate_limit_dep(request: Request):
        """Enforce SOVEREIGN_JOB_RATE_LIMIT_PER_MIN (per client IP). No-op if unset or 0."""
        from fastapi import HTTPException
        limit = int(os.getenv("SOVEREIGN_JOB_RATE_LIMIT_PER_MIN", "0"))
        if limit <= 0:
            return
        now = time.time()
        key = (request.client.host if request.client else None) or "anonymous"
        if key not in _job_rate_limit_times:
            _job_rate_limit_times[key] = []
        times = _job_rate_limit_times[key]
        times[:] = [t for t in times if now - t < 60]
        if len(times) >= limit:
            raise HTTPException(status_code=429, detail="Job creation rate limit exceeded (SOVEREIGN_JOB_RATE_LIMIT_PER_MIN)")
        times.append(now)

    def _job_ip_whitelist_dep(request: Request):
        """When IP whitelist is set (overlay or env), reject requests from other IPs with 403."""
        from fastapi import HTTPException
        o = _get_ui_overrides()
        acc = o.get("access") or {}
        if "ip_whitelist" in acc:
            raw = (acc.get("ip_whitelist") or "").strip()
        else:
            raw = (os.getenv("SOVEREIGN_JOB_IP_WHITELIST") or "").strip()
        if not raw:
            return
        allowed = {x.strip() for x in raw.split(",") if x.strip()}
        if not allowed:
            return
        host = (request.client.host if request.client else None) or ""
        if host not in allowed:
            raise HTTPException(status_code=403, detail="IP not allowed (whitelist)")

    def _validate_job_input(goal: str, amount_cents: int, callback_url: str | None) -> None:
        """Raise HTTPException 400 if goal/amount_cents/callback_url are invalid."""
        from fastapi import HTTPException
        try:
            validate_job_input(goal, amount_cents, callback_url)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.get("/metrics")
    def metrics():
        """Prometheus scrape endpoint: sovereign_jobs_* and LLM/audit metrics."""
        try:
            from sovereign_os.telemetry.tracer import get_prometheus_metrics_output
            from fastapi.responses import Response
            pending = sum(1 for j in _jobs if getattr(j, "status", "") == "pending")
            running = sum(1 for j in _jobs if getattr(j, "status", "") == "running")
            body = get_prometheus_metrics_output(pending=pending, running=running)
            return Response(content=body, media_type="text/plain; charset=utf-8")
        except Exception as e:
            logger.exception("Metrics export failed: %s", e)
            return Response(content=b"# error\n", status_code=500, media_type="text/plain")

    @app.get("/health")
    def health():
        """Health check for load balancers and orchestrators. Returns 200 if ledger is readable; 503 if critical failure. Includes config_warnings and mode hints (auto_approve_jobs, compliance_auto_proceed)."""
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
            jobs_total = len(_jobs)
            jobs_pending = sum(1 for j in _jobs if getattr(j, "status", "") == "pending")
            jobs_running = sum(1 for j in _jobs if getattr(j, "status", "") == "running")
            auto_approve = _effective_auto_approve()
            compliance_auto = _effective_compliance_auto()
            body = {
                "status": status,
                "ledger": ledger_ok,
                "redis": redis_ok,
                "config_warnings": _config_warnings(),
                "jobs_total": jobs_total,
                "jobs_pending": jobs_pending,
                "jobs_running": jobs_running,
                "last_job_completed_at": _last_job_completed_at,
                "auto_approve_jobs": auto_approve,
                "compliance_auto_proceed": compliance_auto,
            }
            return body if ledger_ok else (JSONResponse(status_code=503, content=body))
        except Exception as e:
            logger.exception("Health check failed")
            return JSONResponse(status_code=503, content={"status": "error", "error": str(e)})

    @app.get("/")
    def index():
        from fastapi.responses import Response
        return Response(
            content=_get_dashboard_html(),
            media_type="text/html",
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "Expires": "0",
            },
        )

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
        llm_configured = bool(
            (os.getenv("OPENAI_API_KEY") or "").strip()
            or (os.getenv("ANTHROPIC_API_KEY") or "").strip()
        )
        return {
            "balance": balance,
            "tokens": tokens,
            "trust_score": trust,
            "agent_id": agent_id,
            "charter": _charter_name,
            "llm_configured": llm_configured,
        }

    @app.get("/api/tasks")
    def api_tasks():
        return {"tasks": list(_tasks)}

    @app.get("/api/logs")
    def api_logs():
        return {"logs": [{"source": s, "message": m} for s, m in _logs]}

    @app.get("/api/token_usage")
    def api_token_usage(limit: int = 80):
        """Token consumption per task and per agent (from Ledger). Returns newest first so recent runs show real API usage."""
        out: list[dict[str, Any]] = []
        if _ledger and hasattr(_ledger, "entries"):
            token_entries = [
                (e.seq, e.token)
                for e in _ledger.entries()
                if getattr(e, "token", None) is not None
            ]
            token_entries.sort(key=lambda x: -x[0])
            cap = min(500, max(1, int(limit)))
            for _seq, t in token_entries[:cap]:
                aid = t.agent_id or "—"
                if aid and "-" in aid:
                    aid = aid.split("-")[0].strip() or aid
                task_label = getattr(t, "task_display", "") or t.task_id or "—"
                if not task_label.strip():
                    task_label = t.task_id or "—"
                out.append({
                    "task_id": task_label,
                    "task_id_full": t.task_id or task_label,
                    "agent_id": aid,
                    "model_id": t.model_id,
                    "input_tokens": t.input_tokens,
                    "output_tokens": t.output_tokens,
                    "total_tokens": t.total_tokens,
                    "estimated_usd_cents": getattr(t, "estimated_usd_cents", 0),
                })
        return {"token_usage": out}

    @app.get("/api/audit_trail")
    def api_audit_trail(limit: int = 200):
        """Verifiable audit trail: last N reports (proof_hash, task_id, passed, etc.). Set SOVEREIGN_AUDIT_TRAIL_PATH to enable persistence."""
        try:
            from sovereign_os.auditor.trail import load_audit_trail, verify_report_integrity
        except ImportError:
            return {"audit_trail": [], "message": "auditor.trail not available"}
        path = os.getenv("SOVEREIGN_AUDIT_TRAIL_PATH")
        if not path:
            return {"audit_trail": [], "message": "SOVEREIGN_AUDIT_TRAIL_PATH not set"}
        entries = load_audit_trail(path, limit=min(500, max(1, limit)))
        for e in entries:
            e["verified"] = verify_report_integrity(e)
            # Readable name: task-1-spec_writer -> "spec_writer", else keep task_id
            tid = e.get("task_id", "")
            if tid.startswith("task-") and "-" in tid[5:]:
                e["task_display"] = tid.split("-", 2)[-1]
            else:
                e["task_display"] = tid or "—"
        return {"audit_trail": entries}

    @app.get("/api/audit_trail/export")
    def api_audit_trail_export():
        """Export audit trail as JSON file (download)."""
        from fastapi.responses import Response
        try:
            from sovereign_os.auditor.trail import load_audit_trail, verify_report_integrity
        except ImportError:
            return Response(content="[]", media_type="application/json", status_code=404)
        path = os.getenv("SOVEREIGN_AUDIT_TRAIL_PATH")
        if not path:
            return Response(content="[]", media_type="application/json", status_code=404)
        entries = load_audit_trail(path, limit=2000)
        for e in entries:
            e["verified"] = verify_report_integrity(e)
        import json
        body = json.dumps(entries, indent=2, default=str)
        return Response(
            content=body,
            media_type="application/json",
            headers={"Content-Disposition": "attachment; filename=audit_trail.json"},
        )

    @app.get("/api/charter")
    def api_charter_get():
        """Return current charter as JSON (mission, fiscal_boundaries, core_competencies)."""
        global _engine, _charter_path
        if _engine and getattr(_engine, "_charter", None):
            c = _engine._charter
            return {
                "mission": c.mission,
                "fiscal_boundaries": c.fiscal_boundaries.model_dump(),
                "core_competencies": [x.model_dump() for x in c.core_competencies],
                "success_kpis": [x.model_dump() for x in c.success_kpis],
                "charter_path": _charter_path,
                "writable": bool(_charter_path and Path(_charter_path).exists() and os.access(Path(_charter_path).parent, os.W_OK)),
            }
        if _charter_path and Path(_charter_path).exists():
            from sovereign_os import load_charter
            c = load_charter(_charter_path)
            return {
                "mission": c.mission,
                "fiscal_boundaries": c.fiscal_boundaries.model_dump(),
                "core_competencies": [x.model_dump() for x in c.core_competencies],
                "success_kpis": [x.model_dump() for x in c.success_kpis],
                "charter_path": _charter_path,
                "writable": os.access(Path(_charter_path).parent, os.W_OK),
            }
        return {"mission": "", "fiscal_boundaries": {}, "core_competencies": [], "success_kpis": [], "charter_path": None, "writable": False}

    @app.put("/api/charter")
    def api_charter_put(payload: dict | None = Body(None)):
        """Update charter file (mission, fiscal_boundaries, core_competencies). Restart required for engine to pick up changes."""
        global _charter_path, _engine
        if not _charter_path or not Path(_charter_path).exists():
            from fastapi import HTTPException
            raise HTTPException(status_code=400, detail="charter_path not set or file missing")
        if not os.access(Path(_charter_path).parent, os.W_OK):
            from fastapi import HTTPException
            raise HTTPException(status_code=403, detail="Charter file is not writable")
        payload = payload or {}
        try:
            from sovereign_os import load_charter
            from sovereign_os.models.charter import Charter, FiscalBoundaries, CoreCompetency
            import yaml
            current = load_charter(_charter_path)
            updates: dict[str, Any] = {}
            if "mission" in payload and payload["mission"] is not None:
                updates["mission"] = str(payload["mission"]).strip() or current.mission
            if "fiscal_boundaries" in payload and isinstance(payload["fiscal_boundaries"], dict):
                fb = payload["fiscal_boundaries"]
                updates["fiscal_boundaries"] = current.fiscal_boundaries.model_copy(update={
                    k: (float(fb[k]) if k in ("daily_burn_max_usd", "max_budget_usd") else str(fb[k]) if k == "currency" else fb[k])
                    for k in ("daily_burn_max_usd", "max_budget_usd", "currency") if k in fb
                })
            if "core_competencies" in payload and isinstance(payload["core_competencies"], list):
                updates["core_competencies"] = [CoreCompetency.model_validate(x) for x in payload["core_competencies"] if isinstance(x, dict)]
            current = current.model_copy(update=updates)
            out = current.model_dump()
            raw = yaml.dump(out, default_flow_style=False, allow_unicode=True, sort_keys=False)
            Path(_charter_path).write_text(raw, encoding="utf-8")
            if _engine and getattr(_engine, "_charter", None):
                from sovereign_os.models.charter import Charter
                _engine._charter = Charter.model_validate(out)
            return {"ok": True, "message": "Charter updated. Restart recommended for full effect."}
        except Exception as e:
            from fastapi import HTTPException
            raise HTTPException(status_code=400, detail=str(e))

    @app.get("/api/workers")
    def api_workers():
        """List registered workers (skill name and agent ids)."""
        global _engine
        if not _engine or not getattr(_engine, "_registry", None):
            return {"workers": [], "message": "Engine or registry not available"}
        reg = _engine._registry
        workers: list[dict[str, Any]] = []
        for skill_name, bidders in getattr(reg, "_skill_to_bidders", {}).items():
            agents = [aid for aid, _ in bidders] if bidders else []
            workers.append({"skill": skill_name, "agent_ids": agents})
        return {"workers": workers}

    def _user_workers_dir() -> Path:
        """Resolved path to sovereign_os/agents/user_workers (for generated code)."""
        return Path(__file__).resolve().parent.parent / "agents" / "user_workers"

    _WORKER_TEMPLATE = '''"""
{class_name}: User-defined worker for skill "{skill}".
Generated via Web UI. Edit this file to customize behavior.
"""

from __future__ import annotations

import logging
from sovereign_os.agents.base import BaseWorker, TaskInput, TaskResult

logger = logging.getLogger(__name__)


class {class_name}(BaseWorker):
    """{description}"""

    async def execute(self, task: TaskInput) -> TaskResult:
        desc = (task.description or "").strip() or task.task_id
        if not self.llm:
            return TaskResult(
                task_id=task.task_id,
                success=True,
                output=f"[{{self.__class__.__name__}}] No LLM; echo: {{desc[:200]}}",
                metadata={{"worker": "{class_name}"}},
            )
        prompt = f"Task: {{desc}}"
        try:
            system = (self.system_prompt or "You are a helpful assistant.").strip() or "You are a helpful assistant."
            messages = [
                {{"role": "system", "content": system}},
                {{"role": "user", "content": prompt}},
            ]
            content = await self.llm.chat(messages)
            output = (content or "").strip() or "[No output]"
            return TaskResult(
                task_id=task.task_id,
                success=True,
                output=output[:65536],
                metadata={{"worker": "{class_name}"}},
            )
        except Exception as e:
            logger.exception("{class_name} execute failed: %s", e)
            return TaskResult(
                task_id=task.task_id,
                success=False,
                output=f"[{{self.__class__.__name__}}] Error: {{e}}",
                metadata={{"worker": "{class_name}", "error": str(e)}},
            )
'''

    @app.post("/api/workers/generate")
    def api_workers_generate(payload: dict | None = Body(None)):
        """Generate a new worker Python file in agents/user_workers. Body: skill (str), description (str). Restart to load. Requires OPENAI_API_KEY or ANTHROPIC_API_KEY."""
        import re
        from fastapi import HTTPException
        if not (os.getenv("OPENAI_API_KEY") or "").strip() and not (os.getenv("ANTHROPIC_API_KEY") or "").strip():
            raise HTTPException(
                status_code=400,
                detail="Set OPENAI_API_KEY or ANTHROPIC_API_KEY in .env to generate workers.",
            )
        body = payload or {}
        skill = (body.get("skill") or "").strip().lower()
        description = (body.get("description") or "").strip() or "User-defined worker."
        if not skill:
            raise HTTPException(status_code=400, detail="skill is required")
        if not re.match(r"^[a-z][a-z0-9_]*$", skill):
            raise HTTPException(
                status_code=400,
                detail="skill must be lowercase letters, numbers, underscores (e.g. my_task)",
            )
        allowed_dir = _user_workers_dir()
        allowed_dir.mkdir(parents=True, exist_ok=True)
        out_file = allowed_dir / f"{skill}_worker.py"
        if out_file.exists():
            raise HTTPException(status_code=409, detail=f"Worker already exists: {out_file.name}")
        class_name = "".join(w.capitalize() for w in skill.split("_")) + "Worker"
        try:
            content = _WORKER_TEMPLATE.format(
                class_name=class_name,
                skill=skill,
                description=description.replace('"""', "'"),
            )
            out_file.write_text(content, encoding="utf-8")
        except OSError as e:
            raise HTTPException(status_code=500, detail=f"Could not write file: {e}") from e
        return {"ok": True, "path": str(out_file), "skill": skill, "message": "Restart the server to load this worker."}

    def _effective_setting(key: str) -> str:
        """Return effective value for a setting (overlay then env)."""
        o = _get_ui_overrides()
        s = o.get("settings") or {}
        if key in s and s[key] is not None:
            return str(s[key]).strip()
        return (os.getenv(key) or "").strip()

    @app.get("/api/settings")
    def api_settings():
        """Settings summary. Editable keys (auto_approve, compliance_auto) can be updated via PUT."""
        return {
            "SOVEREIGN_JOB_DB": os.getenv("SOVEREIGN_JOB_DB") or "(default)",
            "SOVEREIGN_LEDGER_PATH": os.getenv("SOVEREIGN_LEDGER_PATH") or "(default)",
            "SOVEREIGN_AUDIT_TRAIL_PATH": os.getenv("SOVEREIGN_AUDIT_TRAIL_PATH") or "(not set)",
            "SOVEREIGN_AUTO_APPROVE_JOBS": _effective_setting("SOVEREIGN_AUTO_APPROVE_JOBS"),
            "SOVEREIGN_COMPLIANCE_AUTO_PROCEED": _effective_setting("SOVEREIGN_COMPLIANCE_AUTO_PROCEED"),
            "SOVEREIGN_API_KEY": "set" if os.getenv("SOVEREIGN_API_KEY") else "not set",
            "SOVEREIGN_JOB_IP_WHITELIST": os.getenv("SOVEREIGN_JOB_IP_WHITELIST") or "(not set)",
            "STRIPE_API_KEY": "set" if os.getenv("STRIPE_API_KEY") else "not set",
            "OPENAI_API_KEY": "set" if os.getenv("OPENAI_API_KEY") else "not set",
            "ANTHROPIC_API_KEY": "set" if os.getenv("ANTHROPIC_API_KEY") else "not set",
            "charter_path": _charter_path or "(not set)",
            "writable": True,
        }

    @app.put("/api/settings")
    def api_settings_put(payload: dict | None = Body(None)):
        """Update editable settings (SOVEREIGN_AUTO_APPROVE_JOBS, SOVEREIGN_COMPLIANCE_AUTO_PROCEED). Auto-approve takes effect on next job; compliance may require restart."""
        body = payload or {}
        allowed = {"SOVEREIGN_AUTO_APPROVE_JOBS", "SOVEREIGN_COMPLIANCE_AUTO_PROCEED"}
        updates = {k: str(v) for k, v in body.items() if k in allowed and v is not None}
        if updates:
            _set_ui_overrides_section("settings", updates)
        return {"ok": True, "message": "Settings updated. Auto-approve applies to new jobs; restart for full compliance effect."}

    @app.get("/api/access")
    def api_access():
        """Access control: effective values from overlay then env. Editable via PUT."""
        o = _get_ui_overrides()
        acc = o.get("access") or {}
        if "api_key_required" in acc:
            api_key_required = bool(acc["api_key_required"])
        else:
            api_key_required = bool((os.getenv("SOVEREIGN_API_KEY") or "").strip())
        if "ip_whitelist" in acc:
            ip_whitelist = (acc.get("ip_whitelist") or "").strip() or None
        else:
            ip_whitelist = (os.getenv("SOVEREIGN_JOB_IP_WHITELIST") or "").strip() or None
        return {"api_key_required": api_key_required, "ip_whitelist": ip_whitelist, "writable": True}

    @app.put("/api/access")
    def api_access_put(payload: dict | None = Body(None)):
        """Update access settings (API key required, IP whitelist). Takes effect immediately."""
        body = payload or {}
        updates = {}
        if "api_key_required" in body:
            updates["api_key_required"] = bool(body["api_key_required"])
        if "ip_whitelist" in body:
            updates["ip_whitelist"] = (body.get("ip_whitelist") or "").strip() or ""
        if updates:
            _set_ui_overrides_section("access", updates)
        return {"ok": True, "message": "Access settings updated."}

    @app.get("/api/jobs")
    def api_jobs(limit: int = 100):
        """List jobs, most recent first. Query param limit (default 100, max 500) caps the number returned."""
        cap = min(500, max(1, int(limit)))
        sorted_jobs = sorted(_jobs, key=lambda x: -x.job_id)
        return {
            "jobs": [asdict(j) for j in sorted_jobs[:cap]],
            "total": len(_jobs),
        }

    @app.delete("/api/jobs/{job_id}")
    def api_jobs_delete(job_id: int):
        """Delete one job by id (any status). Returns 404 if not found."""
        global _jobs, _job_store
        for j in _jobs:
            if j.job_id == job_id:
                _jobs.remove(j)
                if _job_store is not None and hasattr(_job_store, "delete_job"):
                    try:
                        _job_store.delete_job(job_id)
                    except Exception:
                        pass
                return {"ok": True, "job_id": job_id}
        return JSONResponse(status_code=404, content={"error": "Job not found"})

    @app.get("/api/jobs/{job_id}/result")
    def api_jobs_result(job_id: int):
        """Return stored worker delivery (output) for a completed/failed job. For expandable view in UI."""
        key = int(job_id)
        if key not in _job_results:
            logger.info(
                "Job result not found for job_id=%s (stored count=%d, sample ids=%s)",
                job_id, len(_job_results), list(_job_results.keys())[:8],
            )
            return JSONResponse(status_code=404, content={"error": "No result stored for this job"})
        return _job_results[key]

    @app.get("/api/jobs/{job_id}/result/download")
    def api_jobs_result_download(job_id: int, fmt: str = "txt"):
        """Return job result as a downloadable file. fmt=txt (default) or fmt=md."""
        from fastapi.responses import PlainTextResponse
        import re as _re
        key = int(job_id)
        if key not in _job_results:
            return PlainTextResponse(content="No result stored for this job.", status_code=404)
        data = _job_results[key]
        goal = data.get("goal", "") or ""
        combined = data.get("combined_output") or ""

        # Build a clean readable filename from goal (first 6 words, slug-ified)
        words = _re.sub(r"[^\w\s]", "", goal).split()[:6]
        slug = "-".join(w.lower() for w in words if w) or f"job-{job_id}"
        slug = _re.sub(r"-+", "-", slug)[:48]
        ext = "md" if fmt == "md" else "txt"
        filename = f"{slug}.{ext}"

        # Compose content with header
        header = f"# {goal[:200]}\n\nJob #{job_id}\n\n---\n\n" if fmt == "md" else f"{goal[:200]}\n\n---\n\n"
        # Include per-task outputs if available
        tasks = data.get("tasks") or []
        body_parts = []
        if tasks:
            for i, t in enumerate(tasks, 1):
                skill = t.get("skill", "")
                out = (t.get("output") or "").strip()
                if out:
                    if fmt == "md":
                        body_parts.append(f"## Task {i}" + (f" — {skill}" if skill else "") + f"\n\n{out}")
                    else:
                        body_parts.append(f"=== Task {i}" + (f" ({skill})" if skill else "") + f" ===\n\n{out}")
        if combined and combined not in "\n".join(body_parts):
            if fmt == "md":
                body_parts.append(f"## Delivery\n\n{combined}")
            else:
                body_parts.append(f"=== Delivery ===\n\n{combined}")

        separator = "\n\n---\n\n" if fmt == "md" else "\n\n" + "="*60 + "\n\n"
        text = header + separator.join(body_parts) if body_parts else header + "(no output)"
        media_type = "text/markdown" if fmt == "md" else "text/plain"
        return PlainTextResponse(
            content=text,
            media_type=f"{media_type}; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.post("/api/jobs/clear-completed")
    def api_jobs_clear_completed():
        """Remove all completed, failed, and payment_failed jobs from the list and store."""
        global _jobs, _job_store
        to_remove = [j for j in _jobs if getattr(j, "status", "") in ("completed", "failed", "payment_failed")]
        for j in to_remove:
            _jobs.remove(j)
            if _job_store is not None and hasattr(_job_store, "delete_job"):
                try:
                    _job_store.delete_job(j.job_id)
                except Exception:
                    pass
        return {"ok": True, "removed": len(to_remove)}

    # --- Job Progress ---
    @app.get("/api/jobs/{job_id}/progress")
    def api_job_progress(job_id: int):
        """Real-time progress for a running job."""
        key = int(job_id)
        job = next((j for j in _jobs if j.job_id == key), None)
        if not job:
            return JSONResponse(status_code=404, content={"error": "Job not found"})
        if key in _job_progress:
            return {**_job_progress[key], "status": job.status}
        stage_map = {"pending": ("queued", 0), "approved": ("queued", 0), "running": ("executing", 50), "completed": ("done", 100), "failed": ("failed", 100), "payment_failed": ("failed", 100)}
        s, p = stage_map.get(job.status, ("unknown", 0))
        return {"stage": s, "tasks_total": 0, "tasks_done": 0, "current_task": "", "pct": p, "status": job.status}

    # --- Task Retry / Edit ---
    @app.post("/api/jobs/{job_id}/retry")
    def api_job_retry(job_id: int):
        """Re-queue a failed job for execution."""
        key = int(job_id)
        job = next((j for j in _jobs if j.job_id == key), None)
        if not job:
            return JSONResponse(status_code=404, content={"error": "Job not found"})
        if job.status not in ("failed", "payment_failed"):
            return JSONResponse(status_code=400, content={"error": f"Cannot retry job in status '{job.status}'"})
        job.status = "approved"
        job.error = None
        job.updated_ts = time.time()
        retry_count = getattr(job, "retry_count", 0) or 0
        job.retry_count = retry_count + 1
        if _job_store is not None:
            _job_store.update_job(job.job_id, status="approved", error=None)
        _logs.append(("system", f"Job {job.job_id} queued for retry (attempt #{job.retry_count})."))
        return {"ok": True, "job_id": key, "retry_count": job.retry_count}

    @app.put("/api/jobs/{job_id}")
    def api_job_edit(job_id: int, body: dict):
        """Edit a failed/pending job's goal or amount before retrying."""
        key = int(job_id)
        job = next((j for j in _jobs if j.job_id == key), None)
        if not job:
            return JSONResponse(status_code=404, content={"error": "Job not found"})
        if job.status not in ("failed", "payment_failed", "pending"):
            return JSONResponse(status_code=400, content={"error": f"Cannot edit job in status '{job.status}'"})
        if "goal" in body and body["goal"]:
            job.goal = str(body["goal"])[:8000]
        if "amount_cents" in body:
            job.amount_cents = int(body["amount_cents"])
        job.updated_ts = time.time()
        if _job_store is not None:
            _job_store.update_job(job.job_id, status=job.status)
        return {"ok": True, "job": {"job_id": key, "goal": job.goal, "amount_cents": job.amount_cents, "status": job.status}}

    # --- Batch Operations ---
    @app.post("/api/jobs/batch-approve")
    def api_batch_approve(body: dict):
        """Approve multiple jobs at once. body: {job_ids: [1,2,3]}"""
        ids = body.get("job_ids", [])
        approved = []
        for jid in ids:
            job = next((j for j in _jobs if j.job_id == int(jid)), None)
            if job and job.status == "pending":
                job.status = "approved"
                job.updated_ts = time.time()
                if _job_store is not None:
                    _job_store.update_job(job.job_id, status="approved")
                approved.append(job.job_id)
        _logs.append(("system", f"Batch approved {len(approved)} jobs."))
        return {"ok": True, "approved": approved, "count": len(approved)}

    @app.post("/api/jobs/batch-retry")
    def api_batch_retry(body: dict):
        """Retry multiple failed jobs at once. body: {job_ids: [1,2,3]}"""
        ids = body.get("job_ids", [])
        retried = []
        for jid in ids:
            job = next((j for j in _jobs if j.job_id == int(jid)), None)
            if job and job.status in ("failed", "payment_failed"):
                job.status = "approved"
                job.error = None
                job.updated_ts = time.time()
                rc = getattr(job, "retry_count", 0) or 0
                job.retry_count = rc + 1
                if _job_store is not None:
                    _job_store.update_job(job.job_id, status="approved", error=None)
                retried.append(job.job_id)
        _logs.append(("system", f"Batch retried {len(retried)} jobs."))
        return {"ok": True, "retried": retried, "count": len(retried)}

    @app.post("/api/jobs/batch-delete")
    def api_batch_delete(body: dict):
        """Delete multiple jobs at once. body: {job_ids: [1,2,3]}"""
        ids = [int(x) for x in body.get("job_ids", [])]
        deleted = []
        for jid in ids:
            job = next((j for j in _jobs if j.job_id == jid), None)
            if job:
                _jobs.remove(job)
                if _job_store is not None and hasattr(_job_store, "delete_job"):
                    try:
                        _job_store.delete_job(jid)
                    except Exception:
                        pass
                deleted.append(jid)
        return {"ok": True, "deleted": deleted, "count": len(deleted)}

    @app.post("/api/jobs/batch")
    def api_batch_submit(body: dict):
        """Submit multiple jobs at once. body: {jobs: [{goal, amount_cents, charter?}, ...]}"""
        global _next_job_id
        items = body.get("jobs", [])
        created = []
        for item in items[:50]:
            goal = str(item.get("goal", "")).strip()
            if not goal:
                continue
            amount = int(item.get("amount_cents", 0))
            charter = str(item.get("charter", "Default"))
            job = Job(job_id=_next_job_id, goal=goal, charter=charter, amount_cents=amount, currency=item.get("currency", "USD"))
            _next_job_id += 1
            auto = os.getenv("SOVEREIGN_AUTO_APPROVE_JOBS", "true").lower() in ("true", "1", "yes")
            if auto:
                job.status = "approved"
            _jobs.append(job)
            if _job_store is not None:
                try:
                    _job_store.add_job(goal=job.goal, charter=job.charter, amount_cents=job.amount_cents, currency=job.currency, status=job.status)
                except Exception:
                    pass
            created.append({"job_id": job.job_id, "goal": goal[:80], "status": job.status})
        _logs.append(("system", f"Batch submitted {len(created)} jobs."))
        return {"ok": True, "jobs": created, "count": len(created)}

    # --- Templates ---
    @app.get("/api/templates")
    def api_templates():
        """Return built-in job templates."""
        return {"templates": _JOB_TEMPLATES}

    @app.post("/api/jobs/from-template")
    def api_job_from_template(body: dict):
        """Create a job from a template. body: {template_id, variables: {topic: ..., ...}, amount_cents?}"""
        global _next_job_id
        tid = body.get("template_id", "")
        tpl = next((t for t in _JOB_TEMPLATES if t["id"] == tid), None)
        if not tpl:
            return JSONResponse(status_code=404, content={"error": f"Template '{tid}' not found"})
        variables = body.get("variables", {})
        goal = tpl["goal"]
        for k, v in variables.items():
            goal = goal.replace("{" + k + "}", str(v))
        amount = int(body.get("amount_cents", tpl["default_amount"]))
        job = Job(job_id=_next_job_id, goal=goal, charter="Default", amount_cents=amount, currency="USD")
        _next_job_id += 1
        auto = os.getenv("SOVEREIGN_AUTO_APPROVE_JOBS", "true").lower() in ("true", "1", "yes")
        if auto:
            job.status = "approved"
        _jobs.append(job)
        if _job_store is not None:
            try:
                _job_store.add_job(goal=job.goal, charter=job.charter, amount_cents=job.amount_cents, currency=job.currency, status=job.status)
            except Exception:
                pass
        _logs.append(("system", f"Job {job.job_id} created from template '{tpl['name']}'."))
        return {"ok": True, "job": {"job_id": job.job_id, "goal": goal, "status": job.status, "amount_cents": amount}}

    # --- Worker Stats ---
    @app.get("/api/workers/stats")
    def api_worker_stats():
        """Return per-worker performance stats from recent job results."""
        stats: dict[str, dict] = {}
        for jid, data in _job_results.items():
            for t in (data.get("tasks") or []):
                skill = t.get("skill", "unknown")
                if skill not in stats:
                    stats[skill] = {"skill": skill, "total": 0, "success": 0, "fail": 0, "avg_output_len": 0, "_output_lens": []}
                stats[skill]["total"] += 1
                if t.get("success"):
                    stats[skill]["success"] += 1
                else:
                    stats[skill]["fail"] += 1
                stats[skill]["_output_lens"].append(len(t.get("output", "") or ""))
        for s in stats.values():
            lens = s.pop("_output_lens", [])
            s["avg_output_len"] = int(sum(lens) / len(lens)) if lens else 0
            s["success_rate"] = round(s["success"] / s["total"] * 100, 1) if s["total"] else 0
        return {"stats": list(stats.values())}

    # --- Notifications ---
    @app.get("/api/notifications/config")
    def api_notifications_config():
        """Return current notification config."""
        return {
            "email_enabled": bool(os.getenv("SOVEREIGN_SMTP_HOST")),
            "slack_enabled": bool(os.getenv("SOVEREIGN_SLACK_WEBHOOK_URL")),
            "webhook_enabled": bool(os.getenv("SOVEREIGN_WEBHOOK_URL")),
            "email_to": os.getenv("SOVEREIGN_NOTIFY_EMAIL", ""),
            "slack_url": os.getenv("SOVEREIGN_SLACK_WEBHOOK_URL", "")[:30] + "..." if os.getenv("SOVEREIGN_SLACK_WEBHOOK_URL") else "",
        }

    @app.get("/api/storage")
    def api_storage():
        """Summary of stored data (jobs count, paths). For UI storage panel."""
        completed = sum(1 for j in _jobs if getattr(j, "status", "") in ("completed", "failed", "payment_failed"))
        path = _ui_overrides_path()
        return {
            "jobs_total": len(_jobs),
            "jobs_completed_or_failed": completed,
            "ledger_path": getattr(_ledger, "_path", None) if _ledger else None,
            "audit_path": os.getenv("SOVEREIGN_AUDIT_TRAIL_PATH"),
            "overrides_path": str(path) if path.exists() else None,
        }

    @app.get("/api/ledger/export")
    def api_ledger_export():
        """Export ledger entries as JSONL. Returns text/plain."""
        from fastapi.responses import PlainTextResponse
        if not _ledger or not hasattr(_ledger, "entries"):
            return PlainTextResponse("", status_code=404)
        lines = []
        for e in _ledger.entries():
            lines.append(e.model_dump_json())
        return PlainTextResponse("\n".join(lines), media_type="application/x-ndjson")

    @app.post(
        "/api/jobs",
        summary="Create job (pending approval)",
        description="Submit a new job. Body: goal (str), charter (str, optional), amount_cents (int), currency (str), callback_url (str, optional). Rate limit: SOVEREIGN_JOB_RATE_LIMIT_PER_MIN. If SOVEREIGN_API_KEY is set, send X-API-Key or Authorization: Bearer <key>.",
    )
    def api_jobs_create(
        payload: dict | None = Body(None),
        _: None = Depends(_api_key_dependency()),
        __: None = Depends(_job_rate_limit_dep),
        ___: None = Depends(_job_ip_whitelist_dep),
    ):
        body = payload or {}
        goal = str(body.get("goal") or "Summarize the market in one paragraph.").strip()
        charter = str(body.get("charter") or _charter_name or "Default")
        amount_cents = _safe_int(body.get("amount_cents"), 0)
        currency = str(body.get("currency") or "USD")
        callback_url = (body.get("callback_url") or "").strip() or None
        delivery_contact = body.get("delivery_contact")
        if delivery_contact is not None and not isinstance(delivery_contact, dict):
            delivery_contact = None
        priority = _safe_int(body.get("priority"), 0)
        run_after_ts = _safe_float(body.get("run_after_ts"), default=None)
        if run_after_ts is None and body.get("run_after_sec") is not None:
            sec = _safe_float(body.get("run_after_sec"), 0.0)
            if sec is not None:
                run_after_ts = time.time() + sec
        _validate_job_input(goal, amount_cents, callback_url)
        job = _enqueue_job(goal, charter, amount_cents=amount_cents, currency=currency, callback_url=callback_url, delivery_contact=delivery_contact, priority=priority, run_after_ts=run_after_ts)
        return {"job": asdict(job)}

    @app.post(
        "/api/jobs/batch",
        summary="Create multiple jobs (pending approval)",
        description="Body: jobs (array of { goal, charter?, amount_cents?, currency?, callback_url? }). Same validation as POST /api/jobs. Rate limit: one batch counts as one request.",
    )
    def api_jobs_batch(
        payload: dict | None = Body(None),
        _: None = Depends(_api_key_dependency()),
        __: None = Depends(_job_rate_limit_dep),
        ___: None = Depends(_job_ip_whitelist_dep),
    ):
        body = payload or {}
        items = body.get("jobs") or body.get("items") or []
        if not isinstance(items, list) or len(items) > 100:
            from fastapi import HTTPException
            raise HTTPException(status_code=400, detail="jobs must be an array with 1–100 items")
        charter_default = _charter_name or "Default"
        jobs_out: list[dict[str, Any]] = []
        for i, item in enumerate(items):
            if not isinstance(item, dict):
                from fastapi import HTTPException
                raise HTTPException(status_code=400, detail=f"jobs[{i}] must be an object")
            goal = str(item.get("goal") or "").strip() or "Summarize the market in one paragraph."
            charter = str(item.get("charter") or charter_default).strip() or charter_default
            amount_cents = _safe_int(item.get("amount_cents"), 0)
            currency = str(item.get("currency") or "USD")
            callback_url = (item.get("callback_url") or "").strip() or None
            delivery_contact = item.get("delivery_contact")
            if delivery_contact is not None and not isinstance(delivery_contact, dict):
                delivery_contact = None
            priority = _safe_int(item.get("priority"), 0)
            run_after_ts = _safe_float(item.get("run_after_ts"), default=None)
            if run_after_ts is None and item.get("run_after_sec") is not None:
                sec = _safe_float(item.get("run_after_sec"), 0.0)
                if sec is not None:
                    run_after_ts = time.time() + sec
            _validate_job_input(goal, amount_cents, callback_url)
            job = _enqueue_job(goal, charter, amount_cents=amount_cents, currency=currency, callback_url=callback_url, delivery_contact=delivery_contact, priority=priority, run_after_ts=run_after_ts)
            jobs_out.append(asdict(job))
        return {"jobs": jobs_out}

    @app.post("/api/jobs/{job_id}/approve")
    def api_jobs_approve(job_id: int):
        global _job_store
        for j in _jobs:
            if j.job_id == job_id:
                j.status = "approved"
                j.updated_ts = time.time()
                if _job_store is not None:
                    _job_store.update_job(job_id, status="approved")
                    push_approved = getattr(_job_store, "push_approved", None)
                    if callable(push_approved):
                        push_approved(job_id)
                _logs.append(("ceo", f"Job {job_id} approved for execution."))
                return {"job": asdict(j)}
        return JSONResponse(status_code=404, content={"error": "Job not found"})

    @app.post("/api/jobs/{job_id}/retry", summary="Retry a failed or payment_failed job")
    def api_jobs_retry(job_id: int):
        """Set job back to approved for one more run. Only for status failed/payment_failed; respects SOVEREIGN_JOB_MAX_RETRIES (default 2)."""
        from fastapi import HTTPException
        global _job_store
        max_retries = int(os.getenv("SOVEREIGN_JOB_MAX_RETRIES", "2"))
        for j in _jobs:
            if j.job_id == job_id:
                if j.status not in ("failed", "payment_failed"):
                    raise HTTPException(status_code=400, detail=f"Job not retryable (status={j.status})")
                rc = getattr(j, "retry_count", 0)
                if rc >= max_retries:
                    raise HTTPException(status_code=400, detail=f"Max retries ({max_retries}) exceeded")
                j.status = "approved"
                j.error = None
                j.retry_count = rc + 1
                j.updated_ts = time.time()
                if _job_store is not None:
                    _job_store.update_job(job_id, status="approved", error=None, retry_count=j.retry_count)
                    push_approved = getattr(_job_store, "push_approved", None)
                    if callable(push_approved):
                        push_approved(job_id)
                _logs.append(("ceo", f"Job {job_id} retry approved (attempt {j.retry_count}/{max_retries})."))
                return {"job": asdict(j)}
        raise HTTPException(status_code=404, detail="Job not found")

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
    elif (root / "charter.default.yaml").exists():
        path = str(root / "charter.default.yaml")
    elif (root / "charter.example.yaml").exists():
        path = str(root / "charter.example.yaml")
    elif (root / "charters" / "The_Freelancer.yaml").exists():
        path = str(root / "charters" / "The_Freelancer.yaml")
    else:
        raise FileNotFoundError(
            "No charter file found. Add charter.default.yaml or charter.example.yaml in project root or run with --charter path/to/charter.yaml"
        )
    charter = load_charter(path)
    ledger_path = os.getenv("SOVEREIGN_LEDGER_PATH")
    ledger = UnifiedLedger(persist_path=ledger_path) if ledger_path else UnifiedLedger()
    if ledger.total_usd_cents() == 0:
        ledger.record_usd(1000)  # 1000 cents = $10.00 demo balance (seed when empty)
    auth = SovereignAuth()
    audit_trail_path = os.getenv("SOVEREIGN_AUDIT_TRAIL_PATH")
    use_stub_audit = (os.getenv("SOVEREIGN_AUDIT_STUB") or "").strip().lower() in ("1", "true", "yes")
    if use_stub_audit:
        from sovereign_os.auditor.review_engine import StubAuditor
        review = ReviewEngine(charter, judge=StubAuditor(), audit_trail_path=audit_trail_path)
        logger.info("Sovereign-OS: audit uses StubAuditor (any non-empty output passes). Set SOVEREIGN_AUDIT_STUB=false for Judge LLM.")
    else:
        review = ReviewEngine(charter, audit_trail_path=audit_trail_path)
    compliance_hook = None
    spend_threshold_cents = 0
    try:
        raw = os.getenv("SOVEREIGN_COMPLIANCE_SPEND_THRESHOLD_CENTS", "").strip()
        if raw and int(raw) > 0:
            from sovereign_os.compliance import ThresholdComplianceHook

            spend_threshold_cents = int(raw)
            compliance_hook = ThresholdComplianceHook(spend_threshold_cents)
            logger.info("Sovereign-OS: compliance hook enabled (spend threshold=%s cents)", spend_threshold_cents)
    except ValueError:
        pass
    compliance_auto_proceed = (os.getenv("SOVEREIGN_COMPLIANCE_AUTO_PROCEED") or "").strip().lower() in ("1", "true", "yes")
    if compliance_auto_proceed:
        logger.info("Sovereign-OS: human-out-of-loop — compliance auto-proceed enabled (no human approval for high spend).")
    engine = GovernanceEngine(
        charter,
        ledger,
        auth=auth,
        review_engine=review,
        on_event=_on_event,
        compliance_hook=compliance_hook,
        spend_threshold_cents=spend_threshold_cents,
        compliance_auto_proceed=compliance_auto_proceed,
    )

    # Initialize payment service once per process
    global _payment_service
    try:
        _payment_service = create_payment_service()
    except Exception as e:  # pragma: no cover - optional
        logger.warning("PAYMENTS: Failed to initialize payment service: %s", e)
        _payment_service = None

    charter_name = Path(path).stem.replace("_", " ").title()
    redis_url = (os.getenv("REDIS_URL") or "").strip()
    job_db = os.getenv("SOVEREIGN_JOB_DB")
    global _job_store, _jobs, _next_job_id
    if redis_url:
        try:
            from sovereign_os.jobs.redis_store import RedisJobStore
            _job_store = RedisJobStore(redis_url)
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
                    callback_url=row.callback_url,
                    retry_count=getattr(row, "retry_count", 0),
                    request_id=None,
                    priority=getattr(row, "priority", 0),
                    run_after_ts=getattr(row, "run_after_ts", None),
                    delivery_contact=getattr(row, "delivery_contact", None),
                ))
            _next_job_id = max((j.job_id for j in _jobs), default=0) + 1
            logger.info("Sovereign-OS: Redis job store connected (%s jobs)", len(_jobs))
        except Exception as e:
            logger.warning("Redis job store failed, falling back to in-memory queue: %s", e)
            _job_store = None
    elif job_db:
        from sovereign_os.jobs.store import JobStore
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
                callback_url=row.callback_url,
                retry_count=getattr(row, "retry_count", 0),
                request_id=getattr(row, "request_id", None),
                priority=getattr(row, "priority", 0),
                run_after_ts=getattr(row, "run_after_ts", None),
                delivery_contact=getattr(row, "delivery_contact", None),
            ))
        _next_job_id = max((j.job_id for j in _jobs), default=0) + 1
        logger.info("Sovereign-OS: Job store loaded from %s (%s jobs)", job_db, len(_jobs))
    _load_job_results()
    app = create_app(engine=engine, ledger=ledger, auth=auth, charter_name=charter_name, charter_path=path)
    job_worker_enabled = (os.getenv("SOVEREIGN_JOB_WORKER_ENABLED", "true").strip().lower() not in ("0", "false", "off", "no"))
    job_worker_thread: Thread | None = None
    if job_worker_enabled:
        concurrency = max(1, int(os.getenv("SOVEREIGN_JOB_WORKER_CONCURRENCY", "1")))
        if concurrency > 1:
            import threading
            global _job_concurrency_semaphore
            _job_concurrency_semaphore = threading.Semaphore(concurrency)
        job_worker_thread = Thread(target=_job_worker, daemon=False)
        job_worker_thread.start()
        logger.info("Sovereign-OS Web UI: job worker started (24/7). Open http://localhost:%s (or http://127.0.0.1:%s)", port, port)
    else:
        logger.warning("SOVEREIGN_JOB_WORKER_ENABLED=false: job worker NOT started. No jobs will run until you set it to true and restart.")
    if _effective_auto_approve():
        logger.warning("SOVEREIGN_AUTO_APPROVE_JOBS is ON: new jobs run immediately (higher API cost). Set to false in Settings or .env to approve manually.")
    # Pause: no polling (data/PAUSE_INGEST file, or SOVEREIGN_PAUSE_INGEST=true, or SOVEREIGN_INGEST_ENABLED=false, or SOVEREIGN_INGEST_URL empty)
    _data_dir = Path(os.getenv("SOVEREIGN_DATA_DIR", str(Path(__file__).resolve().parents[2] / "data")))
    _pause_file = _data_dir / "PAUSE_INGEST"
    _pause_ingest = _pause_file.exists() or os.getenv("SOVEREIGN_PAUSE_INGEST", "").strip().lower() in ("1", "true", "yes")
    ingest_url = "" if _pause_ingest else os.getenv("SOVEREIGN_INGEST_URL", "").strip()
    ingest_enabled = ingest_url and (os.getenv("SOVEREIGN_INGEST_ENABLED", "true").strip().lower() not in ("0", "false", "off", "no"))
    if ingest_url and not ingest_enabled:
        logger.warning("SOVEREIGN_INGEST_URL is set but SOVEREIGN_INGEST_ENABLED=false: ingest poller NOT started.")
    try:
        if ingest_enabled and ingest_url:
            from sovereign_os.ingest.poller import start_ingest_poller
            def _enqueue_job_for_ingest(goal: str, charter: str, amount_cents: int, currency: str, callback_url: str | None = None, delivery_contact: dict | None = None):
                dedup_sec = 0
                try:
                    raw = os.getenv("SOVEREIGN_INGEST_DEDUP_SEC", "").strip()
                    if raw:
                        dedup_sec = max(0, int(raw))
                except ValueError:
                    pass
                return _enqueue_job(
                    goal, charter, amount_cents, currency,
                    callback_url=callback_url,
                    delivery_contact=delivery_contact,
                    dedup_within_seconds=dedup_sec or None,
                )
            if start_ingest_poller(_enqueue_job_for_ingest):
                logger.warning("Ingest poller started (SOVEREIGN_INGEST_URL). Jobs will be pulled automatically. Unset SOVEREIGN_INGEST_URL to stop and reduce API cost.")
        elif ingest_url:
            pass  # already logged above
    except Exception as e:
        logger.warning("INGEST: could not start poller: %s", e)
    if job_worker_thread is None:
        logger.info("Sovereign-OS Web UI: Open http://localhost:%s (job worker disabled).", port)

    def _sigterm_handler(*args: Any) -> None:
        global _shutdown_requested
        _shutdown_requested = True
        logger.info("SIGTERM received; job worker will finish current job then exit.")

    try:
        signal.signal(signal.SIGTERM, _sigterm_handler)
    except (AttributeError, ValueError):
        pass  # Windows or unsupported

    import uvicorn
    config = uvicorn.Config(app, host=host, port=port, log_level="info")
    server = uvicorn.Server(config)
    server_thread = Thread(target=server.run, daemon=False)
    server_thread.start()
    shutdown_timeout = 120
    while not _shutdown_requested:
        time.sleep(0.5)
    if _shutdown_requested:
        if job_worker_thread is not None:
            logger.info("Shutdown requested; waiting for job worker (max %ss)...", shutdown_timeout)
            job_worker_thread.join(timeout=shutdown_timeout)
            if job_worker_thread.is_alive():
                logger.warning("Job worker did not finish within %ss", shutdown_timeout)
        # Wait for any in-flight jobs (when concurrency > 1) to finish
        for _ in range(shutdown_timeout):
            if sum(1 for j in _jobs if getattr(j, "status", "") == "running") == 0:
                break
            time.sleep(1)
        server.should_exit = True
        server_thread.join(timeout=5)


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
