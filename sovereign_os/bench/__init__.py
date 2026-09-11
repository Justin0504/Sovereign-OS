"""
SovereignBench: adversarial evaluation of the governance layer.

The rest of the test suite asks whether the system works when the agent cooperates.
This package asks the question that actually matters for a governance layer — whether
its guarantees survive an agent that is trying to get around them — and it answers with
an observer (`oracle`) that does not take the system's word for it.
"""

from sovereign_os.bench.oracle import (
    GroundTruthMonitor,
    MissionBounds,
    Observation,
    ObservationKind,
    Violation,
)
from sovereign_os.bench.threat_model import (
    ATTACKS,
    ATTACKS_BY_ID,
    Attack,
    AttackCategory,
    AttackResult,
    ElicitationLevel,
    Invariant,
    attacks_for,
)

__all__ = [
    "ATTACKS", "ATTACKS_BY_ID", "Attack", "AttackCategory", "AttackResult",
    "ElicitationLevel", "GroundTruthMonitor", "Invariant", "MissionBounds",
    "Observation", "ObservationKind", "Violation", "attacks_for",
]
