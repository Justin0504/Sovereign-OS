# Verifiable audit trail & proof_hash

Phase 6a: every `AuditReport` has a **proof_hash** (SHA-256 of a canonical JSON). You can persist reports to a JSONL file and later verify that no entry was tampered with.

## How it works

1. **Canonical payload** — For each report we build a dict: `task_id`, `kpi_name`, `passed`, `score`, `reason`, `suggested_fix`, `timestamp_utc` (ISO). Keys are sorted; the JSON string is UTF-8.
2. **Hash** — `proof_hash = SHA256(canonical_json).hexdigest()` (64 hex chars). It is exposed as `AuditReport.proof_hash` (computed field).
3. **Persistence** — Set `SOVEREIGN_AUDIT_TRAIL_PATH` (or pass `audit_trail_path` into `ReviewEngine`). Each audit appends one JSON line to the file; the line includes `proof_hash`.
4. **Verification** — Given a line (or dict) from the file, recompute the hash from the same canonical fields (ignoring any existing `proof_hash` in the input). If it matches the stored `proof_hash`, the entry is intact.

## JSONL schema (one line per report)

```json
{
  "task_id": "task-1",
  "kpi_name": "task_ok",
  "passed": true,
  "score": 0.9,
  "reason": "Stub verification: output present",
  "suggested_fix": "",
  "timestamp_utc": "2026-03-06T12:00:00.000000+00:00",
  "proof_hash": "a1b2c3..."
}
```

## Verifying integrity (Python)

```python
from sovereign_os.auditor.trail import load_audit_trail, verify_report_integrity

entries = load_audit_trail("/path/to/audit.jsonl", limit=100)
for e in entries:
    ok = verify_report_integrity(e)
    print(f"{e['task_id']} passed={e['passed']} verified={ok}")
```

## API

- **GET /api/audit_trail?limit=200** — Returns `{ "audit_trail": [ ... ], "message": "..." }`. Each entry includes a **verified** boolean (recomputed hash vs stored). Requires `SOVEREIGN_AUDIT_TRAIL_PATH` to be set.

## Export for third parties

- Share the JSONL file (or a subset of lines). The recipient can verify each line with `verify_report_integrity(entry)` and the same canonical rules (see `sovereign_os.auditor.base._audit_report_canonical` and `compute_audit_proof_hash`). No secret keys are involved; verification is deterministic from the public fields.
