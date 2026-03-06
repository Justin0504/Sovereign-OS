"""
Verifiable audit trail: persist AuditReports to JSONL with proof_hash for integrity.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from sovereign_os.auditor.base import AuditReport

logger = logging.getLogger(__name__)


def append_audit_report(path: Path | str, report: AuditReport) -> None:
    """Append one AuditReport to the audit trail file (JSONL). proof_hash is included (computed)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = report.model_dump(mode="json")
    line["timestamp_utc"] = report.timestamp_utc.isoformat()
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(line, ensure_ascii=False) + "\n")
    logger.debug("AUDIT TRAIL: appended %s proof_hash=%s", report.task_id, report.proof_hash[:16])


def load_audit_trail(path: Path | str, limit: int = 500) -> list[dict]:
    """Load last `limit` entries from the audit trail JSONL. Returns list of dicts (proof_hash, task_id, passed, etc.)."""
    path = Path(path)
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").strip().split("\n")
    if not lines:
        return []
    entries = []
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def verify_report_integrity(entry: dict) -> bool:
    """Recompute proof_hash from entry (without proof_hash) and compare. Returns True if intact."""
    import hashlib
    proof = entry.get("proof_hash") or ""
    if not proof:
        return False
    canonical = {
        "task_id": entry.get("task_id", ""),
        "kpi_name": entry.get("kpi_name", ""),
        "passed": bool(entry.get("passed", False)),
        "score": float(entry.get("score", 0)),
        "reason": entry.get("reason", ""),
        "suggested_fix": entry.get("suggested_fix", ""),
        "timestamp_utc": entry.get("timestamp_utc", ""),
    }
    payload = json.dumps(canonical, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest() == proof
