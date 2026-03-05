"""
Vectorized Memory Management: ChromaDB-backed storage of Successful Task Patterns
and Reflections. Injects top-k similar past lessons into Worker system prompt.
"""

import logging
from datetime import datetime, timezone
from typing import Any

from sovereign_os.memory.schema import MemoryEntry, ReflectionObject

logger = logging.getLogger(__name__)

try:
    import chromadb
    from chromadb.config import Settings as ChromaSettings
    CHROMADB_AVAILABLE = True
except ImportError:
    CHROMADB_AVAILABLE = False
    chromadb = None  # type: ignore[assignment]

COLLECTION_NAME = "sovereign_task_memory"
MAX_RAW_OUTPUT_LEN = 2000


class MemoryManager:
    """
    Persist successful task patterns and audit-failure reflections.
    Query by task description to get top-k similar past lessons for context injection.
    """

    def __init__(self, persist_path: str | None = None) -> None:
        self._persist_path = persist_path
        self._client = None
        self._collection = None
        self._in_memory: list[dict[str, Any]] = []  # fallback when ChromaDB missing
        if CHROMADB_AVAILABLE and chromadb is not None:
            try:
                self._client = chromadb.PersistentClient(
                    path=persist_path or "./data/sovereign_memory",
                    settings=ChromaSettings(anonymized_telemetry=False),
                )
                self._collection = self._client.get_or_create_collection(
                    name=COLLECTION_NAME,
                    metadata={"description": "Task patterns and reflections"},
                )
                logger.info("MEMORY: ChromaDB persistent store ready at %s", persist_path or "./data/sovereign_memory")
            except Exception as e:
                logger.warning("MEMORY: ChromaDB init failed (%s); using in-memory fallback.", e)
                self._client = None
                self._collection = None
        else:
            logger.warning("MEMORY: ChromaDB not installed; using in-memory fallback. pip install chromadb")

    def add_success(
        self,
        task_id: str,
        agent_id: str,
        audit_score: float,
        kpi_target: str,
        raw_output: str,
        lessons_learned: str = "",
    ) -> None:
        """Store a successful task pattern for future similarity retrieval."""
        raw_truncated = raw_output[:MAX_RAW_OUTPUT_LEN] if raw_output else ""
        entry = MemoryEntry(
            timestamp=datetime.now(timezone.utc),
            agent_id=agent_id,
            audit_score=audit_score,
            kpi_target=kpi_target,
            raw_output=raw_truncated,
            lessons_learned=lessons_learned or "Task completed successfully.",
            is_reflection=False,
        )
        doc_text = f"Task: {task_id}. {entry.to_document_text()}"
        meta = {
            "task_id": task_id,
            "agent_id": entry.agent_id,
            "audit_score": entry.audit_score,
            "kpi_target": entry.kpi_target,
            "raw_output": entry.raw_output,
            "timestamp": entry.timestamp.isoformat(),
            "is_reflection": "false",
        }
        if self._collection is not None:
            try:
                self._collection.add(
                    documents=[doc_text],
                    metadatas=[meta],
                    ids=[f"success_{task_id}_{entry.timestamp.timestamp()}"],
                )
            except Exception as e:
                logger.warning("MEMORY: add_success failed: %s", e)
        else:
            self._in_memory.append({"doc": doc_text, "meta": meta, "lessons": entry.lessons_learned})

    def add_reflection(self, reflection: ReflectionObject) -> None:
        """Persist a reflection (audit failure) with high priority so the mistake is not repeated."""
        lessons = reflection.to_lessons_learned()
        raw_truncated = (reflection.raw_output or "")[:MAX_RAW_OUTPUT_LEN]
        meta = {
            "task_id": reflection.task_id,
            "agent_id": reflection.agent_id,
            "audit_score": reflection.audit_score,
            "kpi_target": reflection.kpi_name,
            "raw_output": raw_truncated,
            "is_reflection": "true",
            "failure_reason": reflection.failure_reason,
            "corrected_logic": reflection.corrected_logic,
        }
        doc_text = f"Reflection. {lessons}"
        if self._collection is not None:
            try:
                from datetime import datetime, timezone
                ts = datetime.now(timezone.utc).isoformat()
                meta["timestamp"] = ts
                self._collection.add(
                    documents=[doc_text],
                    metadatas=[meta],
                    ids=[f"refl_{reflection.task_id}_{reflection.agent_id}_{ts}"],
                )
                logger.info("MEMORY: Reflection saved (high priority) for task %s", reflection.task_id)
            except Exception as e:
                logger.warning("MEMORY: add_reflection failed: %s", e)
        else:
            meta["timestamp"] = datetime.now(timezone.utc).isoformat()
            self._in_memory.append({"doc": doc_text, "meta": meta, "lessons": lessons})

    def get_similar_lessons(self, task_description: str, k: int = 3) -> list[str]:
        """
        Return top-k similar past lessons (success or reflection) for the given task.
        Used to inject into Worker system prompt before execution.
        """
        if self._collection is not None:
            try:
                results = self._collection.query(
                    query_texts=[task_description],
                    n_results=min(k, max(1, self._collection.count())),
                    include=["documents", "metadatas"],
                )
                if results and results.get("documents") and results["documents"][0]:
                    # Prefer reflections (is_reflection=true) by ordering or metadata filter if supported
                    docs = results["documents"][0]
                    metadatas = results.get("metadatas", [[]])[0] or []
                    out = []
                    for i, doc in enumerate(docs):
                        meta = metadatas[i] if i < len(metadatas) else {}
                        # Extract lessons from doc or meta
                        if isinstance(doc, str) and ("Lessons:" in doc or "Corrected" in doc):
                            out.append(doc)
                        else:
                            out.append(doc)
                        if len(out) >= k:
                            break
                    return out[:k]
            except Exception as e:
                logger.warning("MEMORY: get_similar_lessons query failed: %s", e)
        # In-memory fallback: simple substring match or return last k
        if not self._in_memory:
            return []
        desc_lower = task_description.lower()
        scored = []
        for item in self._in_memory:
            doc = item.get("doc", "")
            lessons = item.get("lessons", doc)
            score = sum(1 for w in desc_lower.split() if len(w) > 2 and w in doc.lower())
            scored.append((score, lessons))
        scored.sort(key=lambda x: -x[0])
        return [s[1] for s in scored[:k]]
