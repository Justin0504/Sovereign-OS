"""
SQLite-backed Job store: persist job queue and approval state across restarts.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass
class JobRow:
    """One job row (matches web Job minus created_ts/updated_ts as floats)."""

    job_id: int
    goal: str
    charter: str
    amount_cents: int = 0
    currency: str = "USD"
    status: str = "pending"
    created_ts: float = 0.0
    updated_ts: float = 0.0
    payment_id: str | None = None
    error: str | None = None
    callback_url: str | None = None
    retry_count: int = 0
    priority: int = 0
    run_after_ts: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class JobStore:
    """
    Persist jobs to SQLite. Use SOVEREIGN_JOB_DB to enable (e.g. /app/data/jobs.db).
    """

    def __init__(self, db_path: str | Path) -> None:
        self._path = Path(db_path) if db_path != ":memory:" else db_path
        if self._path != ":memory:" and isinstance(self._path, Path):
            self._path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _conn(self) -> sqlite3.Connection:
        if self._path == ":memory:":
            return sqlite3.connect("file::memory:?cache=shared", uri=True, check_same_thread=False)
        return sqlite3.connect(str(self._path), check_same_thread=False)

    def _init_schema(self) -> None:
        with self._conn() as c:
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    goal TEXT NOT NULL,
                    charter TEXT NOT NULL,
                    amount_cents INTEGER NOT NULL DEFAULT 0,
                    currency TEXT NOT NULL DEFAULT 'USD',
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_ts REAL NOT NULL,
                    updated_ts REAL NOT NULL,
                    payment_id TEXT,
                    error TEXT,
                    callback_url TEXT,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    priority INTEGER NOT NULL DEFAULT 0,
                    run_after_ts REAL
                )
                """
            )
            c.execute(
                "CREATE INDEX IF NOT EXISTS ix_jobs_status ON jobs(status)"
            )
            # Migration: add callback_url if table existed without it
            if self._path != ":memory:":
                try:
                    c.execute("ALTER TABLE jobs ADD COLUMN callback_url TEXT")
                except sqlite3.OperationalError:
                    pass  # column already exists
                try:
                    c.execute("ALTER TABLE jobs ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0")
                except sqlite3.OperationalError:
                    pass  # column already exists
                try:
                    c.execute("ALTER TABLE jobs ADD COLUMN priority INTEGER NOT NULL DEFAULT 0")
                except sqlite3.OperationalError:
                    pass
                try:
                    c.execute("ALTER TABLE jobs ADD COLUMN run_after_ts REAL")
                except sqlite3.OperationalError:
                    pass

    def list_jobs(self) -> list[JobRow]:
        with self._conn() as c:
            c.row_factory = sqlite3.Row
            rows = c.execute(
                "SELECT job_id, goal, charter, amount_cents, currency, status, created_ts, updated_ts, payment_id, error, callback_url, retry_count, priority, run_after_ts FROM jobs ORDER BY job_id"
            ).fetchall()
        def _row(r: Any) -> JobRow:
            keys = r.keys() if hasattr(r, "keys") else []
            return JobRow(
                job_id=r["job_id"],
                goal=r["goal"],
                charter=r["charter"],
                amount_cents=r["amount_cents"],
                currency=r["currency"],
                status=r["status"],
                created_ts=r["created_ts"],
                updated_ts=r["updated_ts"],
                payment_id=r["payment_id"],
                error=r["error"],
                callback_url=r["callback_url"] if "callback_url" in keys else None,
                retry_count=int(r["retry_count"]) if "retry_count" in keys else 0,
                priority=int(r["priority"]) if "priority" in keys else 0,
                run_after_ts=float(r["run_after_ts"]) if "run_after_ts" in keys and r["run_after_ts"] is not None else None,
            )
        return [_row(r) for r in rows]

    def next_job_id(self) -> int:
        with self._conn() as c:
            r = c.execute("SELECT COALESCE(MAX(job_id), 0) + 1 AS n FROM jobs").fetchone()
            return r[0]

    def add_job(
        self,
        goal: str,
        charter: str,
        amount_cents: int = 0,
        currency: str = "USD",
        callback_url: str | None = None,
        priority: int = 0,
        run_after_ts: float | None = None,
    ) -> JobRow:
        now = time.time()
        with self._conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO jobs (goal, charter, amount_cents, currency, status, created_ts, updated_ts, callback_url, retry_count, priority, run_after_ts)
                VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, 0, ?, ?)
                """,
                (goal, charter, amount_cents, currency, now, now, callback_url or None, priority, run_after_ts),
            )
            job_id = cur.lastrowid if cur.lastrowid else self.next_job_id()
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
            callback_url=callback_url or None,
            retry_count=0,
            priority=priority,
            run_after_ts=run_after_ts,
        )

    def get_job(self, job_id: int) -> JobRow | None:
        with self._conn() as c:
            c.row_factory = sqlite3.Row
            r = c.execute(
                "SELECT job_id, goal, charter, amount_cents, currency, status, created_ts, updated_ts, payment_id, error, callback_url, retry_count, priority, run_after_ts FROM jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        if r is None:
            return None
        keys = r.keys()
        return JobRow(
            job_id=r["job_id"],
            goal=r["goal"],
            charter=r["charter"],
            amount_cents=r["amount_cents"],
            currency=r["currency"],
            status=r["status"],
            created_ts=r["created_ts"],
            updated_ts=r["updated_ts"],
            payment_id=r["payment_id"],
            error=r["error"],
            callback_url=r["callback_url"] if "callback_url" in keys else None,
            retry_count=int(r["retry_count"]) if "retry_count" in keys else 0,
            priority=int(r["priority"]) if "priority" in keys else 0,
            run_after_ts=float(r["run_after_ts"]) if "run_after_ts" in keys and r["run_after_ts"] is not None else None,
        )

    def update_job(
        self,
        job_id: int,
        *,
        status: str | None = None,
        payment_id: str | None = None,
        error: str | None = None,
        retry_count: int | None = None,
    ) -> bool:
        updates: list[str] = ["updated_ts = ?"]
        args: list[Any] = [time.time()]
        if status is not None:
            updates.append("status = ?")
            args.append(status)
        if payment_id is not None:
            updates.append("payment_id = ?")
            args.append(payment_id)
        if error is not None:
            updates.append("error = ?")
            args.append(error)
        if retry_count is not None:
            updates.append("retry_count = ?")
            args.append(retry_count)
        args.append(job_id)
        with self._conn() as conn:
            cur = conn.execute(
                f"UPDATE jobs SET {', '.join(updates)} WHERE job_id = ?",
                args,
            )
            return cur.rowcount > 0

    def delete_job(self, job_id: int) -> bool:
        """Remove a job by id. Returns True if deleted."""
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM jobs WHERE job_id = ?", (job_id,))
            return cur.rowcount > 0
