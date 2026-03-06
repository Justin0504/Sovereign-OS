# Backup: Job queue and Ledger

For disaster recovery, back up the SQLite job DB and the ledger JSONL regularly.

## What to back up

| Path (env var) | Description |
|----------------|-------------|
| `SOVEREIGN_JOB_DB` (e.g. `./data/jobs.db`) | SQLite DB: job queue, status, payment_id, retry_count. |
| `SOVEREIGN_LEDGER_PATH` (e.g. `./data/ledger.jsonl`) | Append-only ledger: USD and token entries. |
| `SOVEREIGN_AUDIT_TRAIL_PATH` (optional) | Audit reports with proof_hash. |

## Simple backup script (host)

```bash
#!/bin/bash
# Backup job DB and ledger (run from project root or set DATA_DIR)
DATA_DIR="${SOVEREIGN_DATA_DIR:-./data}"
BACKUP_DIR="${BACKUP_DIR:-./backups}"
mkdir -p "$BACKUP_DIR"
STAMP=$(date +%Y%m%d-%H%M%S)
[ -f "$DATA_DIR/jobs.db" ] && cp -a "$DATA_DIR/jobs.db" "$BACKUP_DIR/jobs.db.$STAMP"
[ -f "$DATA_DIR/ledger.jsonl" ] && cp -a "$DATA_DIR/ledger.jsonl" "$BACKUP_DIR/ledger.jsonl.$STAMP"
echo "Backup done: $BACKUP_DIR/*.$STAMP"
```

## Docker volume backup

If you use Docker Compose and the `sovereign_data` volume:

```bash
docker compose run --rm -v sovereign_data:/data -v $(pwd)/backups:/backup app \
  sh -c "cp -a /data/jobs.db /backup/jobs.db.$(date +%Y%m%d) 2>/dev/null; cp -a /data/ledger.jsonl /backup/ledger.jsonl.$(date +%Y%m%d) 2>/dev/null; echo done"
```

Adjust service name and paths if your compose differs.

## Restore

- **Ledger:** Replace `ledger.jsonl` with the backup file; ensure the process is stopped so the file is not appended during restore.
- **Job DB:** Replace `jobs.db` with the backup; restart the Web process. Jobs that were running at backup time may need to be retried via `POST /api/jobs/{id}/retry` if they were interrupted.

## Frequency

Recommendation: daily (e.g. cron) for production. Retain at least 7 days of backups.
