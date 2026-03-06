"""
Phase 6b: Sovereign identity and compliance hooks (design stubs).

- SovereignIdentity: stable ID for entity/agent; optional on-chain anchor.
- ComplianceHook: check(action_type, context) -> Allow | Deny | RequestHumanApproval.
- OnChainSettlement: submit_settlement(...) -> tx_hash (design).

See docs/PHASE6.md for the full design.
"""

from sovereign_os.compliance.identity import SovereignIdentity, StubIdentity
from sovereign_os.compliance.hooks import ComplianceHook, ComplianceResult, StubComplianceHook, ThresholdComplianceHook
from sovereign_os.compliance.settlement import OnChainSettlement, StubOnChainSettlement

__all__ = [
    "SovereignIdentity",
    "StubIdentity",
    "ComplianceHook",
    "ComplianceResult",
    "StubComplianceHook",
    "ThresholdComplianceHook",
    "OnChainSettlement",
    "StubOnChainSettlement",
]
