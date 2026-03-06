# Multi-instance and horizontal scaling

By default, the job queue is stored in **SQLite** (`SOVEREIGN_JOB_DB`). A single Web process runs the job worker; with `SOVEREIGN_JOB_WORKER_CONCURRENCY` you can run several jobs in parallel on that same process.

## Single instance (current)

- One Web process = one writer to the SQLite DB.
- **Concurrency:** Set `SOVEREIGN_JOB_WORKER_CONCURRENCY=2` (or higher) to run multiple jobs in parallel on that instance.
- **Backup:** See [BACKUP.md](BACKUP.md) for backing up the job DB and ledger.

## Multiple instances (Redis queue)

When **`REDIS_URL`** is set, the app uses **Redis** as the job store and approved queue. Any number of Web processes can run; workers claim the next approved job via `BRPOP` on `sovereign:queue:approved`. Install with `pip install -e ".[deploy]"` (adds `redis`). Jobs are stored in Redis hashes; approval pushes the job id to the shared list so any instance can pop and run it.

| Env | Effect |
|-----|--------|
| `REDIS_URL` | Use Redis for job storage and approved queue; enables multi-instance workers. |
| `SOVEREIGN_JOB_DB` | Used only when `REDIS_URL` is not set; SQLite path for single-instance persistence. |

## Single worker + multiple API servers (SQLite)

To run **multiple Web processes** with SQLite (no Redis), use a **single worker process** that has `SOVEREIGN_JOB_DB` and the worker loop, and one or more **API-only** processes (no worker) that share the same DB path over a shared volume. Only the single worker should write. Prefer Redis or one instance with `SOVEREIGN_JOB_WORKER_CONCURRENCY` for simplicity.
