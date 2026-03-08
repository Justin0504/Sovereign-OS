"""
Redis-backed Job store for multi-instance: shared queue and job state.
Use when REDIS_URL is set; workers claim jobs via BRPOP from sovereign:queue:approved.
"""

from __future__ import annotations

import json
import time
from typing import Any

from sovereign_os.jobs.store import JobRow


def _float_or_none(v: Any) -> float | None:
    if v is None or v == "" or v == b"":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _safe_int(v: Any, default: int = 0) -> int:
    if v is None or v == "" or v == b"":
        return default
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _safe_float(v: Any, default: float = 0.0) -> float:
    if v is None or v == "" or v == b"":
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


class RedisJobStore:
    """
    Store jobs in Redis. Same interface as JobStore plus push_approved / pop_approved
    for the worker to claim the next approved job across instances.
    """

    KEY_NEXT_ID = "sovereign:job:next_id"
    KEY_IDS = "sovereign:job:ids"
    KEY_JOB = "sovereign:job:{id}"
    QUEUE_APPROVED = "sovereign:queue:approved"

    def __init__(self, redis_url: str, key_prefix: str = "sovereign") -> None:
        import redis
        self._client = redis.from_url(redis_url, decode_responses=True)
        self._prefix = key_prefix or "sovereign"
        self._key_next = f"{self._prefix}:job:next_id"
        self._key_ids = f"{self._prefix}:job:ids"
        self._key_job = f"{self._prefix}:job:{{id}}"
        self._queue_approved = f"{self._prefix}:queue:approved"

    def _job_key(self, job_id: int) -> str:
        return self._key_job.format(id=job_id)

    def next_job_id(self) -> int:
        return self._client.incr(self._key_next)

    def add_job(
        self,
        goal: str,
        charter: str,
        amount_cents: int = 0,
        currency: str = "USD",
        callback_url: str | None = None,
        delivery_contact: dict | None = None,
        priority: int = 0,
        run_after_ts: float | None = None,
    ) -> JobRow:
        job_id = self.next_job_id()
        now = time.time()
        dc_json = json.dumps(delivery_contact) if isinstance(delivery_contact, dict) else ""
        data = {
            "job_id": str(job_id),
            "goal": goal,
            "charter": charter,
            "amount_cents": str(amount_cents),
            "currency": currency,
            "status": "pending",
            "created_ts": str(now),
            "updated_ts": str(now),
            "payment_id": "",
            "error": "",
            "callback_url": "" if callback_url is None else callback_url,
            "retry_count": "0",
            "priority": str(priority),
            "run_after_ts": "" if run_after_ts is None else str(run_after_ts),
            "delivery_contact": dc_json,
        }
        pipe = self._client.pipeline()
        pipe.hset(self._job_key(job_id), mapping=data)
        pipe.sadd(self._key_ids, job_id)
        pipe.execute()
        return JobRow(
            job_id=job_id,
            goal=goal,
            charter=charter,
            amount_cents=amount_cents,
            currency=currency,
            status="pending",
            created_ts=now,
            updated_ts=now,
            payment_id=None,
            error=None,
            callback_url=callback_url,
            retry_count=0,
            priority=priority,
            run_after_ts=run_after_ts,
            delivery_contact=delivery_contact,
        )

    def _hgetrow(self, job_id: int) -> dict[str, Any] | None:
        raw = self._client.hgetall(self._job_key(job_id))
        if not raw:
            return None
        out: dict[str, Any] = {}
        for k, v in raw.items():
            if k in ("job_id", "amount_cents", "retry_count", "priority"):
                try:
                    out[k] = int(v) if v else 0
                except ValueError:
                    out[k] = 0
            elif k in ("created_ts", "updated_ts", "run_after_ts"):
                out[k] = _float_or_none(v)
            elif k in ("payment_id", "error", "callback_url"):
                out[k] = v or None
            elif k == "delivery_contact":
                if v and isinstance(v, str):
                    try:
                        out[k] = json.loads(v)
                    except (json.JSONDecodeError, TypeError):
                        out[k] = None
                else:
                    out[k] = None
            else:
                out[k] = v
        return out

    def get_job(self, job_id: int) -> JobRow | None:
        row = self._hgetrow(job_id)
        if not row:
            return None
        return JobRow(
            job_id=_safe_int(row.get("job_id", job_id), job_id),
            goal=str(row.get("goal", "")),
            charter=str(row.get("charter", "")),
            amount_cents=_safe_int(row.get("amount_cents", 0), 0),
            currency=str(row.get("currency", "USD")),
            status=str(row.get("status", "pending")),
            created_ts=_safe_float(row.get("created_ts", 0), 0.0),
            updated_ts=_safe_float(row.get("updated_ts", 0), 0.0),
            payment_id=row.get("payment_id"),
            error=row.get("error"),
            callback_url=row.get("callback_url"),
            retry_count=_safe_int(row.get("retry_count", 0), 0),
            priority=_safe_int(row.get("priority", 0), 0),
            run_after_ts=_float_or_none(row.get("run_after_ts")),
            delivery_contact=row.get("delivery_contact"),
        )

    def list_jobs(self) -> list[JobRow]:
        ids = self._client.smembers(self._key_ids)
        out: list[JobRow] = []
        for jid in sorted(int(i) for i in ids):
            job = self.get_job(jid)
            if job:
                out.append(job)
        return out

    def update_job(
        self,
        job_id: int,
        *,
        status: str | None = None,
        payment_id: str | None = None,
        error: str | None = None,
        retry_count: int | None = None,
    ) -> bool:
        key = self._job_key(job_id)
        if not self._client.exists(key):
            return False
        updates: dict[str, str] = {"updated_ts": str(time.time())}
        if status is not None:
            updates["status"] = status
        if payment_id is not None:
            updates["payment_id"] = payment_id
        if error is not None:
            updates["error"] = error
        if retry_count is not None:
            updates["retry_count"] = str(retry_count)
        self._client.hset(key, mapping=updates)
        return True

    def push_approved(self, job_id: int) -> None:
        """Add job_id to the approved queue (call when approving a job)."""
        self._client.lpush(self._queue_approved, str(job_id))

    def pop_approved(self, timeout: float = 5.0) -> int | None:
        """Block and claim the next approved job_id. Returns None on timeout or empty queue."""
        result = self._client.brpop(self._queue_approved, timeout=timeout)
        if not result:
            return None
        _, job_id_str = result
        try:
            return int(job_id_str)
        except (ValueError, TypeError):
            return None
