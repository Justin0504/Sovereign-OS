"""
Phase 6b: Sovereign identity (design).

Stable, verifiable identity for the entity or each agent.
Can be attached to Charter or Worker; Auditor/Ledger can record which identity performed an action.
"""

from abc import ABC, abstractmethod
from typing import Any


class SovereignIdentity(ABC):
    """
    Stable identity for the sovereign entity or an agent.
    Future: DID, or internal UUID + optional on-chain anchor.
    """

    @property
    @abstractmethod
    def id(self) -> str:
        """Stable unique identifier (e.g. UUID or DID)."""
        ...

    @property
    def on_chain_anchor(self) -> str | None:
        """Optional on-chain address or commitment; None if not yet anchored."""
        return None

    def to_dict(self) -> dict[str, Any]:
        """Serialize for audit/logging."""
        return {"id": self.id, "on_chain_anchor": self.on_chain_anchor}


class StubIdentity(SovereignIdentity):
    """Minimal implementation for testing and default wiring."""

    def __init__(self, id: str = "sovereign-stub-1", on_chain_anchor: str | None = None):
        self._id = id
        self._on_chain_anchor = on_chain_anchor

    @property
    def id(self) -> str:
        return self._id

    @property
    def on_chain_anchor(self) -> str | None:
        return self._on_chain_anchor
