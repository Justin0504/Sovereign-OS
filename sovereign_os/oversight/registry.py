"""
OversightRegistry: a small store of outbound escrows Sovereign-OS has posted, so
the poller can settle them, the dashboard can show them, and the CLI can drive
them. Append/update in memory with optional JSON persistence.

Status lifecycle: funded -> delivered -> released | disputed
(rejected is recorded when the budget gate blocks a post before funding).
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class EscrowRecord:
    escrow_id: str
    title: str
    price_cents: int
    status: str = "funded"           # funded|delivered|released|disputed|rejected
    platform: str = "rentahuman"
    bounty_id: str = ""
    required_skill: str = "general"
    completion_criteria: str = ""
    score: float | None = None
    reason: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class OversightRegistry:
    def __init__(self, persist_path: str | Path | None = None) -> None:
        self._records: dict[str, EscrowRecord] = {}
        self._path = Path(persist_path) if persist_path else None
        if self._path and self._path.exists():
            self._load()

    def add(self, record: EscrowRecord) -> EscrowRecord:
        self._records[record.escrow_id] = record
        self._save()
        return record

    def update(self, escrow_id: str, **fields) -> EscrowRecord | None:
        rec = self._records.get(escrow_id)
        if rec is None:
            return None
        for k, v in fields.items():
            if hasattr(rec, k):
                setattr(rec, k, v)
        self._save()
        return rec

    def get(self, escrow_id: str) -> EscrowRecord | None:
        return self._records.get(escrow_id)

    def list(self, status: str | None = None) -> list[EscrowRecord]:
        recs = list(self._records.values())
        return [r for r in recs if r.status == status] if status else recs

    def to_dicts(self) -> list[dict]:
        return [r.to_dict() for r in self._records.values()]

    def summary(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in self._records.values():
            out[r.status] = out.get(r.status, 0) + 1
        return out

    # ------------------------------------------------------- persistence
    def _save(self) -> None:
        if not self._path:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(self.to_dicts()), encoding="utf-8")
        except Exception as e:  # pragma: no cover - best-effort
            logger.warning("OVERSIGHT REGISTRY: save failed: %s", e)

    def _load(self) -> None:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))  # type: ignore[union-attr]
            for d in data:
                rec = EscrowRecord(**{k: v for k, v in d.items() if k in EscrowRecord.__dataclass_fields__})
                self._records[rec.escrow_id] = rec
        except Exception as e:  # pragma: no cover - best-effort
            logger.warning("OVERSIGHT REGISTRY: load failed: %s", e)
