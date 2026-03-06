"""
Phase 6b: On-chain settlement (design).

Minimal interface for recording income/payouts on-chain.
UnifiedLedger could delegate to an OnChainSettlement adapter for verifiable settlement.
"""

from abc import ABC, abstractmethod
from typing import Literal


TxType = Literal["income", "payout", "memo"]


class OnChainSettlement(ABC):
    """
    Submit a settlement event to the chain (e.g. stablecoin transfer or memo).
    Returns tx_hash for audit; Ledger can record it alongside the amount.
    """

    @abstractmethod
    def submit_settlement(
        self,
        tx_type: TxType,
        amount_cents: int,
        reference_id: str,
        destination: str | None = None,
    ) -> str:
        """
        Submit settlement. Returns transaction hash (or placeholder if not implemented).
        destination: required for payout; optional for income/memo.
        """
        ...


class StubOnChainSettlement(OnChainSettlement):
    """No-op implementation; returns a placeholder hash. Use real adapter in production."""

    def submit_settlement(
        self,
        tx_type: TxType,
        amount_cents: int,
        reference_id: str,
        destination: str | None = None,
    ) -> str:
        return f"0xstub-{tx_type}-{reference_id}-{amount_cents}"
