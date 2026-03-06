"""
Sovereign-OS: A General-Purpose Autonomous Corporation Framework.

Charter-driven entity instantiation with fiscal-aware orchestration,
dynamic permissioning, and multi-step audit loops.
"""

__version__ = "0.3.0"

from sovereign_os.ledger import UnifiedLedger
from sovereign_os.models.charter import Charter, load_charter

__all__ = [
    "__version__",
    "Charter",
    "load_charter",
    "UnifiedLedger",
]
