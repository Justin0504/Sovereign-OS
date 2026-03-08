"""
UnifiedLedger: Immutable append-only ledger tracking every cent and every token.

All financial (USD) and token expenditures flow through this single source of truth
for P&L, burn rate, runway, and audit.
"""

from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Entry types
# ---------------------------------------------------------------------------


class EntryKind(str, Enum):
    """Type of ledger entry."""

    USD_DEBIT = "usd_debit"   # Money spent
    USD_CREDIT = "usd_credit" # Money received
    TOKEN_DEBIT = "token_debit"


class USDEntry(BaseModel):
    """A single USD (cent-level) movement."""

    amount_cents: Annotated[int, Field(description="Amount in cents (signed)")]
    currency: str = "USD"
    agent_id: str | None = None
    purpose: str = ""
    ref: str = ""  # External reference (e.g. task_id, invoice_id)
    timestamp_utc: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    model_config = {"frozen": True}


class TokenEntry(BaseModel):
    """A single token usage record (input + output)."""

    model_id: str  # e.g. "gpt-4o", "o1"
    input_tokens: Annotated[int, Field(ge=0)] = 0
    output_tokens: Annotated[int, Field(ge=0)] = 0
    agent_id: str | None = None
    task_id: str = ""
    task_display: str = ""  # Optional short label from goal (e.g. "Cold outreach copy") for UI
    estimated_usd_cents: Annotated[int, Field(ge=0)] = 0  # Optional cost tracking
    timestamp_utc: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    model_config = {"frozen": True}

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class LedgerEntry(BaseModel):
    """One immutable ledger row: either USD or Token."""

    kind: EntryKind
    seq: Annotated[int, Field(ge=0)]  # Monotonic sequence number
    usd: USDEntry | None = None
    token: TokenEntry | None = None

    model_config = {"frozen": True}

    @classmethod
    def create_usd(cls, seq: int, usd_entry: USDEntry) -> "LedgerEntry":
        kind = EntryKind.USD_DEBIT if usd_entry.amount_cents < 0 else EntryKind.USD_CREDIT
        return cls(kind=kind, seq=seq, usd=usd_entry, token=None)

    @classmethod
    def create_token(cls, seq: int, token_entry: TokenEntry) -> "LedgerEntry":
        return cls(kind=EntryKind.TOKEN_DEBIT, seq=seq, usd=None, token=token_entry)


# ---------------------------------------------------------------------------
# UnifiedLedger
# ---------------------------------------------------------------------------


class UnifiedLedger:
    """
    Append-only ledger for all monetary and token flows.

    Thread-safe usage: callers should serialize writes (e.g. single writer).
    Persistence: optional file-backed append log.
    """

    __slots__ = ("_entries", "_seq", "_path", "_dirty", "_loaded_count")

    def __init__(self, persist_path: str | Path | None = None) -> None:
        self._entries: list[LedgerEntry] = []
        self._seq = 0
        self._path = Path(persist_path) if persist_path else None
        self._dirty = False
        self._loaded_count = 0
        if self._path and self._path.exists():
            self._load()

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _load(self) -> None:
        """Load existing entries from disk (append log)."""
        if not self._path:
            return
        from pydantic import TypeAdapter

        adapter = TypeAdapter(LedgerEntry)
        with open(self._path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                entry = adapter.validate_json(line)
                self._entries.append(entry)
                self._seq = max(self._seq, entry.seq)
        self._loaded_count = len(self._entries)
        self._dirty = False

    def _flush(self) -> None:
        """Append new entries to persist_path if set."""
        if not self._path or not self._dirty:
            return
        with open(self._path, "a", encoding="utf-8") as f:
            for i in range(self._loaded_count, len(self._entries)):
                f.write(self._entries[i].model_dump_json() + "\n")
        self._loaded_count = len(self._entries)
        self._dirty = False

    def record_usd(self, amount_cents: int, *, agent_id: str | None = None, purpose: str = "", ref: str = "") -> LedgerEntry:
        """Record a USD movement (positive = credit, negative = debit)."""
        entry = LedgerEntry.create_usd(
            self._next_seq(),
            USDEntry(amount_cents=amount_cents, agent_id=agent_id, purpose=purpose, ref=ref),
        )
        self._entries.append(entry)
        self._dirty = True
        self._flush()
        return entry

    def record_token(
        self,
        model_id: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        *,
        agent_id: str | None = None,
        task_id: str = "",
        task_display: str = "",
        estimated_usd_cents: int = 0,
    ) -> LedgerEntry:
        """Record token consumption."""
        entry = LedgerEntry.create_token(
            self._next_seq(),
            TokenEntry(
                model_id=model_id,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                agent_id=agent_id,
                task_id=task_id,
                task_display=task_display,
                estimated_usd_cents=estimated_usd_cents,
            ),
        )
        self._entries.append(entry)
        self._dirty = True
        self._flush()
        return entry

    def total_usd_cents(self) -> int:
        """Net USD in cents (positive = surplus, negative = deficit)."""
        total = 0
        for e in self._entries:
            if e.usd:
                total += e.usd.amount_cents
        return total

    def total_tokens_by_model(self) -> dict[str, int]:
        """Total tokens consumed per model_id."""
        out: dict[str, int] = {}
        for e in self._entries:
            if e.token:
                out[e.token.model_id] = out.get(e.token.model_id, 0) + e.token.total_tokens
        return out

    def total_token_estimated_usd_cents(self) -> int:
        """Sum of estimated USD (cents) for all token entries."""
        return sum(e.token.estimated_usd_cents for e in self._entries if e.token)

    def usd_debits_since(self, since: datetime) -> int:
        """Total USD spent (debits only) since given time. For daily burn."""
        total = 0
        for e in self._entries:
            if e.usd and e.usd.amount_cents < 0 and e.usd.timestamp_utc >= since:
                total += abs(e.usd.amount_cents)
        return total

    def runway_days(self, daily_burn_cents: int) -> int | None:
        """
        Estimated runway in days given current balance and a fixed daily burn.
        Returns None if daily_burn_cents <= 0 or balance is negative (infinite/no runway).
        """
        balance = self.total_usd_cents()
        if daily_burn_cents <= 0:
            return None
        if balance <= 0:
            return 0
        return balance // daily_burn_cents

    def entries(self) -> list[LedgerEntry]:
        """Read-only view of all entries (for audit / dashboard)."""
        return list(self._entries)
